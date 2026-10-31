"""
AI Chatbot Blueprint for EcoCycle Admin Dashboard
==================================================
HOW TO UNDO (removes all AI chatbot features):
  1. Delete this file  (website/ai_chat.py)
  2. Delete            website/templates/ai_chat_widget.html
  3. Delete            website/static/js/ai_chat.js
  4. In website/__init__.py  → remove the 2 lines marked  # [AI-CHATBOT]
  5. In admin_dashboard.html → remove the 1 line marked   {# [AI-CHATBOT] #}
Nothing else in the project is touched.
"""

from flask import Blueprint, request, jsonify, session
from google import genai
import os, re

ai_chat_bp = Blueprint("ai_chat", __name__)

# ── Gemini setup ──────────────────────────────────────────────────────────────
# NOTE: read fresh each call so it works even if env var is set after import
_client = None

def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable not set.")
        _client = genai.Client(api_key=api_key)
    return _client


# ── DB helper (uses the mysql object passed in during registration) ────────────
_mysql = None

def init_mysql(mysql_instance):
    global _mysql
    _mysql = mysql_instance


def _fetch_live_stats():
    """Pull a compact snapshot of EcoCycle DB for the AI context."""
    if _mysql is None:
        return {}
    try:
        import MySQLdb.cursors
        cur = _mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        def q(sql, args=()):
            cur.execute(sql, args)
            return cur.fetchall()

        def scalar(sql, args=()):
            cur.execute(sql, args)
            r = cur.fetchone()
            return list(r.values())[0] if r else 0

        stats = {}

        # ── High-level counts ───────────────────────────────────────────────
        stats["total_households"]   = scalar("SELECT COUNT(*) FROM household_users WHERE registration_status='Approved'")
        stats["pending_households"] = scalar("SELECT COUNT(*) FROM household_users WHERE registration_status='Pending'")
        stats["total_workers"]      = scalar("SELECT COUNT(*) FROM workers")
        stats["total_staff"]        = scalar("SELECT COUNT(*) FROM staff")

        # ── Today's pickups ─────────────────────────────────────────────────
        stats["completed_today"]    = scalar("SELECT COUNT(*) FROM pickup_requests WHERE status='Completed' AND completed_date=CURDATE()")
        stats["pending_today"]      = scalar("SELECT COUNT(*) FROM pickup_requests WHERE status='Pending' AND DATE(created_at)=CURDATE()")
        stats["rejected_today"]     = scalar("SELECT COUNT(*) FROM pickup_requests WHERE status IN ('Not at Home','Not Home') AND DATE(updated_at)=CURDATE()")

        # ── Waste totals ─────────────────────────────────────────────────────
        stats["total_waste_kg"]     = float(scalar("SELECT COALESCE(SUM(weight_kg),0) FROM reward_logs") or 0)
        stats["waste_today_kg"]     = float(scalar("SELECT COALESCE(SUM(weight_kg),0) FROM reward_logs WHERE DATE(created_at)=CURDATE()") or 0)

        # ── Redeem / rewards ─────────────────────────────────────────────────
        stats["pending_redeem"]     = scalar("SELECT COUNT(*) FROM redeem_requests WHERE status='Pending'")

        # ── Per-ward summary ─────────────────────────────────────────────────
        wards = q("""
            SELECT base.ward_no,
                (SELECT COUNT(*) FROM workers w WHERE w.ward_no=base.ward_no) as workers,
                (SELECT COUNT(*) FROM household_users hu WHERE hu.ward_no=base.ward_no AND hu.registration_status='Approved') as households,
                (SELECT COUNT(*) FROM pickup_requests pr JOIN household_users hu ON pr.household_id=hu.household_id WHERE hu.ward_no=base.ward_no AND pr.status='Completed' AND pr.completed_date=CURDATE()) as collected,
                (SELECT COUNT(*) FROM pickup_requests pr JOIN household_users hu ON pr.household_id=hu.household_id WHERE hu.ward_no=base.ward_no AND pr.status='Pending' AND DATE(pr.created_at)=CURDATE()) as pending
            FROM (SELECT DISTINCT ward_no FROM household_users WHERE ward_no IS NOT NULL
                  UNION SELECT DISTINCT ward_no FROM workers WHERE ward_no IS NOT NULL) base
            ORDER BY base.ward_no
        """)
        stats["wards"] = [dict(r) for r in wards]

        # ── Top workers today ────────────────────────────────────────────────
        top_workers = q("""
            SELECT w.full_name, w.ward_no,
                (SELECT COUNT(*) FROM pickup_requests pr WHERE pr.worker_id=w.worker_id AND pr.status='Completed' AND pr.completed_date=CURDATE()) as completed,
                (SELECT COALESCE(SUM(c.weight_kg),0) FROM reward_logs c WHERE c.worker_id=w.worker_id AND DATE(c.created_at)=CURDATE()) as kg_today
            FROM workers w ORDER BY completed DESC, kg_today DESC LIMIT 5
        """)
        stats["top_workers"] = [dict(r) for r in top_workers]

        # ── Waste by category ────────────────────────────────────────────────
        cats = q("""
            SELECT wc.category_name, COALESCE(SUM(rl.weight_kg),0) as total_kg
            FROM waste_categories wc LEFT JOIN reward_logs rl ON rl.category_id=wc.category_id
            GROUP BY wc.category_id, wc.category_name ORDER BY total_kg DESC
        """)
        stats["waste_by_category"] = [dict(r) for r in cats]

        # ── Recent feedbacks ─────────────────────────────────────────────────
        feedbacks = q("""
            SELECT f.type, f.message, f.status, h.full_name as household
            FROM feedbacks f JOIN household_users h ON f.household_id=h.household_id
            ORDER BY f.created_at DESC LIMIT 5
        """)
        stats["recent_feedbacks"] = [dict(r) for r in feedbacks]

        cur.close()
        return stats
    except Exception as e:
        print("AI chatbot DB error:", e)
        return {}


def _build_system_prompt(stats: dict) -> str:
    ward_lines = "\n".join(
        f"  - Ward {w['ward_no']}: {w['households']} households, {w['workers']} workers, "
        f"{w['collected']} collected today, {w['pending']} pending today"
        for w in stats.get("wards", [])
    )
    worker_lines = "\n".join(
        f"  - {w['full_name']} (Ward {w['ward_no']}): {w['completed']} completed, {float(w['kg_today']):.1f} kg today"
        for w in stats.get("top_workers", [])
    )
    cat_lines = "\n".join(
        f"  - {c['category_name']}: {float(c['total_kg']):.1f} kg total"
        for c in stats.get("waste_by_category", [])
    )
    feedback_lines = "\n".join(
        f"  - [{r['type']}] {r['household']}: \"{r['message'][:80]}\" ({r['status']})"
        for r in stats.get("recent_feedbacks", [])
    )

    return f"""You are EcoBot, an AI assistant embedded in the EcoCycle waste management admin dashboard.
EcoCycle is a waste management platform that handles household waste pickups, worker assignments,
recycling tracking, reward points for households, and inventory management.

You have access to the following LIVE data snapshot from the database:

=== SYSTEM OVERVIEW ===
- Approved Households : {stats.get('total_households', 'N/A')}
- Pending Households  : {stats.get('pending_households', 'N/A')}
- Workers             : {stats.get('total_workers', 'N/A')}
- Staff               : {stats.get('total_staff', 'N/A')}
- Pending Redeem Reqs : {stats.get('pending_redeem', 'N/A')}

=== TODAY'S PICKUP STATS ===
- Completed  : {stats.get('completed_today', 'N/A')}
- Pending    : {stats.get('pending_today', 'N/A')}
- Rejected   : {stats.get('rejected_today', 'N/A')}
- Waste today: {stats.get('waste_today_kg', 0):.1f} kg

=== ALL-TIME WASTE ===
- Total collected: {stats.get('total_waste_kg', 0):.1f} kg

=== WARD-WISE SUMMARY ===
{ward_lines if ward_lines else "  (no ward data)"}

=== TOP WORKERS TODAY ===
{worker_lines if worker_lines else "  (no worker data)"}

=== WASTE BY CATEGORY ===
{cat_lines if cat_lines else "  (no category data)"}

=== RECENT FEEDBACKS ===
{feedback_lines if feedback_lines else "  (no feedbacks)"}

INSTRUCTIONS:
- Answer admin questions about operations, workers, wards, waste, feedback, etc.
- Be concise and data-driven. Use the live data above.
- If you don't have data for something, say so honestly.
- Format numbers clearly. Use bullet points for lists.
- You are friendly but professional.
"""


# ── Route ─────────────────────────────────────────────────────────────────────
@ai_chat_bp.route("/admin/ai_chat", methods=["POST"])
def ai_chat():
    if "admin_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    user_msg = (data.get("message") or "").strip()
    if not user_msg:
        return jsonify({"error": "Empty message"}), 400

    # Basic safety — no prompt injection
    if len(user_msg) > 500:
        return jsonify({"reply": "Please keep your message under 500 characters."}), 200

    try:
        client = _get_client()
        stats  = _fetch_live_stats()
        prompt = _build_system_prompt(stats)

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=f"{prompt}\n\nAdmin question: {user_msg}"
        )
        reply = response.text.strip()
        return jsonify({"reply": reply})

    except RuntimeError as e:
        return jsonify({"reply": f"⚠️ {e}"})
    except Exception as e:
        import traceback
        traceback.print_exc()   # prints full trace to your terminal
        return jsonify({"reply": f"❌ Error: {str(e)}"})

