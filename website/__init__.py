
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_mysqldb import MySQL
import os, re
from werkzeug.utils import secure_filename
from datetime import datetime
from datetime import datetime, timedelta
import MySQLdb.cursors

def create_app():
    app = Flask(__name__)
    app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "static", "uploads", "aadhaar")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    app.config["PAYMENT_UPLOAD_FOLDER"] = os.path.join(app.root_path, "static", "uploads", "payments")
    os.makedirs(app.config["PAYMENT_UPLOAD_FOLDER"], exist_ok=True)
    app.config['SECRET_KEY'] = 'ecocycle123'
    app.config['MYSQL_HOST'] = '127.0.0.1'
    app.config['MYSQL_USER'] = 'root'
    app.config['MYSQL_PASSWORD'] = 'Ecocycle@123'
    app.config['MYSQL_DB'] = 'ecocycle'
    mysql = MySQL(app)

    def get_available_worker(cursor, ward_no, area, exclude_worker_id):
        """Get alternate worker: same area -> same ward -> return None"""
        try:
            cursor.execute("SELECT festive_mode FROM system_config WHERE id=1")
            f = cursor.fetchone()
            limit_day = 35 if f and f.get('festive_mode')==1 else 5
        except:
            limit_day = 5

        # 1. Same Ward + Same Area, not on leave, under limit, within shift
        cursor.execute("""
            SELECT w.worker_id, w.full_name,
                (SELECT COUNT(*) FROM pickup_requests pr 
                 WHERE pr.worker_id=w.worker_id 
                 AND pr.status IN ('Pending','Assigned') 
                 AND DATE(pr.created_at)=CURDATE()) as active_cnt
            FROM workers w
            WHERE w.ward_no=%s 
              AND REPLACE(LOWER(TRIM(w.area)),' ','')=REPLACE(LOWER(TRIM(%s)),' ','')
              AND w.worker_id != %s
              AND w.worker_id NOT IN (SELECT worker_id FROM worker_leaves WHERE leave_date=CURDATE() AND status IN ('Accepted','Approved','Pending'))
              AND CURTIME() BETWEEN w.shift_start AND w.shift_end
            HAVING active_cnt < LEAST(w.daily_limit, %s)
            ORDER BY active_cnt ASC LIMIT 1
        """, (ward_no, area, exclude_worker_id, limit_day))
        rw = cursor.fetchone()
        if rw: return rw

        # 2. Same Ward, any area
        cursor.execute("""
            SELECT w.worker_id, w.full_name,
                (SELECT COUNT(*) FROM pickup_requests pr 
                 WHERE pr.worker_id=w.worker_id AND pr.status IN ('Pending','Assigned') AND DATE(pr.created_at)=CURDATE()) as active_cnt
            FROM workers w
            WHERE w.ward_no=%s AND w.worker_id != %s
              AND w.worker_id NOT IN (SELECT worker_id FROM worker_leaves WHERE leave_date=CURDATE() AND status IN ('Accepted','Approved','Pending'))
            HAVING active_cnt < LEAST(w.daily_limit, %s)
            ORDER BY active_cnt ASC LIMIT 1
        """, (ward_no, exclude_worker_id, limit_day))
        return cursor.fetchone()

    def auto_reassign_expired_requests(cursor):
        # Called from admin_dashboard and admin_notify_leave
        cursor.execute("""
            SELECT wl.worker_id, wl.ward_no, wl.area 
            FROM worker_leaves wl
            WHERE wl.leave_date = CURDATE() AND wl.status IN ('Accepted','Approved')
        """)
        for lw in cursor.fetchall():
            cursor.execute("""
                SELECT id, household_id FROM pickup_requests 
                WHERE worker_id=%s AND status IN ('Pending','Assigned') AND DATE(created_at)=CURDATE()
            """, (lw['worker_id'],))
            for pr in cursor.fetchall():
                new_w = get_available_worker(cursor, lw['ward_no'], lw['area'], lw['worker_id'])
                if new_w:
                    cursor.execute("UPDATE pickup_requests SET worker_id=%s, status='Pending', reassign_count=reassign_count+1, is_reassigned=1, updated_at=NOW() WHERE id=%s", (new_w['worker_id'], pr['id']))
                    cursor.execute("INSERT INTO reassign_logs (pickup_id, household_id, old_worker_id, new_worker_id, reason) VALUES (%s,%s,%s,%s,%s)",
                                   (pr['id'], pr['household_id'], lw['worker_id'], new_w['worker_id'], f'Auto reassign - Worker on leave'))
                else:
                    cursor.execute("UPDATE pickup_requests SET worker_id=NULL, status='Pending', reassign_count=reassign_count+1, is_reassigned=1, updated_at=NOW() WHERE id=%s", (pr['id'],))



    def get_todays_requests_data(cursor):
        try:
            cursor.execute("""
                SELECT pr.id, pr.status, pr.created_at, pr.completed_date, pr.updated_at,
                    hu.household_id, hu.full_name as household_name, hu.house_no, hu.ward_no, hu.area, hu.phone,
                    w.worker_id, w.full_name as worker_name, w.phone as worker_phone,
                    GROUP_CONCAT(DISTINCT wc.category_name SEPARATOR ', ') as waste_type,
                    GROUP_CONCAT(CONCAT(c.weight_kg + 0, ' kg (', wc.category_name, ')') SEPARATOR ', ') as weight_details,
                    COALESCE(SUM(c.weight_kg), 0) as weight_kg,
                    COALESCE(SUM(c.points), 0) as points,
                    (SELECT COUNT(*) FROM worker_leaves wl 
                     WHERE (wl.worker_id = pr.worker_id OR (pr.worker_id IS NULL AND wl.worker_id IN (
                         SELECT w2.worker_id FROM workers w2 
                         WHERE w2.ward_no = hu.ward_no 
                           AND REPLACE(LOWER(TRIM(w2.area)),' ','') = REPLACE(LOWER(TRIM(hu.area)),' ','')
                     )))
                     AND wl.leave_date = CURDATE()) as is_worker_on_leave
                FROM pickup_requests pr
                JOIN household_users hu ON hu.household_id=pr.household_id
                LEFT JOIN workers w ON w.worker_id=pr.worker_id
                LEFT JOIN reward_logs c ON c.pickup_id=pr.id
                LEFT JOIN waste_categories wc ON wc.category_id=c.category_id
                WHERE DATE(pr.created_at)=CURDATE() OR DATE(pr.updated_at)=CURDATE() OR pr.completed_date=CURDATE()
                GROUP BY pr.id, pr.status, pr.created_at, pr.completed_date, pr.updated_at,
                         hu.household_id, hu.full_name, hu.house_no, hu.ward_no, hu.area, hu.phone,
                         w.worker_id, w.full_name, w.phone
                ORDER BY pr.created_at DESC
            """)
            requests = cursor.fetchall()
            now = datetime.now()
            for r in requests:
                created = r.get('created_at')
                r['is_worker_on_leave'] = bool(r.get('is_worker_on_leave', 0) > 0)
                if r.get('status') == 'Pending' and created:
                    if isinstance(created, str):
                        try:
                            created = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
                        except:
                            pass
                    if isinstance(created, datetime):
                        r['is_stuck_over_1hr'] = (now - created).total_seconds() >= 3600
                    else:
                        r['is_stuck_over_1hr'] = False
                else:
                    r['is_stuck_over_1hr'] = False
            return requests
        except Exception as e:
            print("get_todays_requests_data error", e)
            return []

    # ... rest of helper functions ...

            
    def get_inventory_status(stock_kg, stock_count, unit):
        kg  = float(stock_kg  or 0)
        cnt = int(stock_count or 0)
        if kg < 1 and cnt < 1:
            return 'EMPTY'
        if unit == 'kg':
            if 1 <= kg <= 10:
                return 'LOW'
        else:
            if 1 <= cnt <= 20:
                return 'LOW'
            if cnt == 0 and 1 <= kg <= 10:
                return 'LOW'
        return 'OK'

    @app.route("/", methods=["GET", "POST"])
    @app.route("/login", methods=["GET", "POST"])
    def home():
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            if not email or not password:
                flash("Please enter both email and password.", "error")
                return redirect(url_for("home"))
            if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
                flash("Please enter a valid email address.", "error")
                return redirect(url_for("home"))
            cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cursor.execute("SELECT * FROM admin WHERE email=%s AND password=%s", (email, password))
            admin = cursor.fetchone()
            if admin:
                session.clear()
                session["admin_id"] = admin['admin_id']
                session["admin_name"] = admin['full_name'] if 'full_name' in admin else admin['email']
                cursor.close()
                return redirect(url_for("admin_dashboard"))
            cursor.execute("SELECT * FROM household_users WHERE email=%s AND password=%s", (email, password))
            household = cursor.fetchone()
            if household:
                status = household['registration_status']
                if status == "Pending":
                    flash("Your registration request is still pending for admin approval.", "warning")
                    cursor.close()
                    return redirect(url_for("home"))
                elif status == "Rejected":
                    flash("Your registration was rejected. Contact admin.", "error")
                    cursor.close()
                    return redirect(url_for("home"))
                elif status == "Approved":
                    session.clear()
                    session["household_id"] = household['household_id']
                    session["household_name"] = household['full_name']
                    cursor.close()
                    return redirect(url_for("household_dashboard"))
            cursor.execute("SELECT * FROM workers WHERE email=%s AND password=%s", (email, password))
            worker = cursor.fetchone()
            if worker:
                session.clear()
                session["worker_id"] = worker['worker_id']
                session["worker_name"] = worker['full_name']
                session["worker_area"] = worker['area']
                session["worker_ward"] = worker['ward_no']
                cursor.close()
                return redirect(url_for("worker_dashboard"))
            cursor.execute("SELECT * FROM staff WHERE email=%s AND password=%s", (email, password))
            staff = cursor.fetchone()
            if staff:
                session.clear()
                session["staff_id"] = staff['staff_id']
                session["staff_name"] = staff['full_name']
                cursor.close()
                return redirect(url_for("recycling_dashboard"))
            cursor.close()
            flash("Invalid Email or Password", "error")
            return redirect(url_for("home"))
        return render_template("login.html",)

    @app.route("/forgot_password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not email or not re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email):
                flash("Please enter a valid registered email address.", "error")
                return render_template("forgot_password.html")

            if len(password) < 6:
                flash("New password must be at least 6 characters long.", "error")
                return render_template("forgot_password.html")

            if password != confirm_password:
                flash("Passwords do not match.", "error")
                return render_template("forgot_password.html")

            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            
            # 1. Check household_users
            cur.execute("SELECT household_id FROM household_users WHERE email=%s", (email,))
            if cur.fetchone():
                cur.execute("UPDATE household_users SET password=%s WHERE email=%s", (password, email))
                mysql.connection.commit()
                cur.close()
                flash("Password updated successfully! Please log in with your new password.", "success")
                return redirect(url_for("home"))

            # 2. Check workers
            cur.execute("SELECT worker_id FROM workers WHERE email=%s", (email,))
            if cur.fetchone():
                cur.execute("UPDATE workers SET password=%s WHERE email=%s", (password, email))
                mysql.connection.commit()
                cur.close()
                flash("Password updated successfully! Please log in with your new password.", "success")
                return redirect(url_for("home"))

            # 3. Check staff
            cur.execute("SELECT staff_id FROM staff WHERE email=%s", (email,))
            if cur.fetchone():
                cur.execute("UPDATE staff SET password=%s WHERE email=%s", (password, email))
                mysql.connection.commit()
                cur.close()
                flash("Password updated successfully! Please log in with your new password.", "success")
                return redirect(url_for("home"))

            # 4. Check admin
            cur.execute("SELECT admin_id FROM admin WHERE email=%s", (email,))
            if cur.fetchone():
                cur.execute("UPDATE admin SET password=%s WHERE email=%s", (password, email))
                mysql.connection.commit()
                cur.close()
                flash("Password updated successfully! Please log in with your new password.", "success")
                return redirect(url_for("home"))

            cur.close()
            flash("No account registered with this email address.", "error")
            return render_template("forgot_password.html")

        return render_template("forgot_password.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip()
            phone = request.form.get("phone", "").strip()
            upi_id = request.form.get("upi_id", "").strip() or None
            ward_no = request.form.get("ward_no", "").strip()
            house_no = request.form.get("house_no", "").strip()
            latitude = request.form.get("latitude", "").strip()
            longitude = request.form.get("longitude", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            # Server-side validations
            if len(full_name) < 2:
                flash("Full name must be at least 2 characters long.", "error")
                cursor.close()
                return redirect(url_for("register"))
            
            if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
                flash("Please enter a valid email address.", "error")
                cursor.close()
                return redirect(url_for("register"))

            if not re.match(r'^[6-9]\d{9}$', phone):
                flash("Please enter a valid 10-digit mobile number starting with 6-9.", "error")
                cursor.close()
                return redirect(url_for("register"))

            if upi_id and not re.match(r'^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}$', upi_id):
                flash("Please enter a valid UPI ID (e.g. name@upi).", "error")
                cursor.close()
                return redirect(url_for("register"))

            if not house_no:
                flash("House number is required.", "error")
                cursor.close()
                return redirect(url_for("register"))

            if len(password) < 6:
                flash("Password must be at least 6 characters long.", "error")
                cursor.close()
                return redirect(url_for("register"))

            if password != confirm_password:
                flash("Passwords do not match.", "error")
                cursor.close()
                return redirect(url_for("register"))

            aadhaar_photo = request.files.get("aadhaar_photo")
            if not aadhaar_photo or not aadhaar_photo.filename:
                flash("Please upload a valid Aadhaar document/photo.", "error")
                cursor.close()
                return redirect(url_for("register"))

            allowed_extensions = ('.jpg', '.jpeg', '.png', '.webp', '.pdf')
            if not aadhaar_photo.filename.lower().endswith(allowed_extensions):
                flash("Aadhaar photo must be JPG, PNG, WEBP, or PDF format.", "error")
                cursor.close()
                return redirect(url_for("register"))

            # Ensure database-level unique constraints exist
            try:
                cursor.execute("ALTER TABLE household_users ADD CONSTRAINT unique_phone UNIQUE (phone)")
            except Exception: pass
            try:
                cursor.execute("ALTER TABLE household_users ADD CONSTRAINT unique_upi UNIQUE (upi_id)")
            except Exception: pass
            try:
                cursor.execute("ALTER TABLE household_users ADD CONSTRAINT unique_ward_house UNIQUE (ward_no, house_no)")
            except Exception: pass

            # 1. Phone Number Uniqueness Validation
            cursor.execute(
                "SELECT household_id FROM household_users WHERE phone=%s UNION SELECT worker_id FROM workers WHERE phone=%s UNION SELECT staff_id FROM staff WHERE phone=%s",
                (phone, phone, phone)
            )
            if cursor.fetchone():
                flash("This phone number is already registered.", "error")
                cursor.close()
                return redirect(url_for("register"))

            # 2. UPI ID Uniqueness Validation
            if upi_id:
                cursor.execute(
                    "SELECT household_id FROM household_users WHERE upi_id=%s",
                    (upi_id,)
                )
                if cursor.fetchone():
                    flash("This UPI ID is already registered.", "error")
                    cursor.close()
                    return redirect(url_for("register"))

            # 3. Email Uniqueness Validation
            cursor.execute(
                "SELECT household_id FROM household_users WHERE LOWER(email)=LOWER(%s) UNION SELECT worker_id FROM workers WHERE LOWER(email)=LOWER(%s) UNION SELECT staff_id FROM staff WHERE LOWER(email)=LOWER(%s)",
                (email, email, email)
            )
            if cursor.fetchone():
                flash("This email address is already registered.", "error")
                cursor.close()
                return redirect(url_for("register"))

            # 4. House Number + Ward Composite Uniqueness Validation
            if house_no and ward_no:
                cursor.execute(
                    "SELECT household_id FROM household_users WHERE ward_no=%s AND UPPER(TRIM(house_no))=UPPER(TRIM(%s))",
                    (ward_no, house_no)
                )
                if cursor.fetchone():
                    flash("This house number is already registered in this ward.", "error")
                    cursor.close()
                    return redirect(url_for("register"))

            filename = secure_filename(aadhaar_photo.filename)
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            aadhaar_photo.save(save_path)
            photo_path = "uploads/aadhaar/" + filename

            try:
                cursor.execute(
                    "INSERT INTO household_users (full_name, email, phone, ward_no, house_no, upi_id, aadhaar_photo, latitude, longitude, password, registration_status, reward_wallet) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (full_name, email, phone, ward_no, house_no, upi_id, photo_path, latitude, longitude, password, 'Pending', 0)
                )
                mysql.connection.commit()
            except Exception as err:
                mysql.connection.rollback()
                err_msg = str(err).lower()
                if "unique_phone" in err_msg or "phone" in err_msg:
                    flash("This phone number is already registered.", "error")
                elif "unique_upi" in err_msg or "upi_id" in err_msg or "upi" in err_msg:
                    flash("This UPI ID is already registered.", "error")
                elif "email" in err_msg:
                    flash("This email address is already registered.", "error")
                elif "unique_ward_house" in err_msg or "house_no" in err_msg or "ward_no" in err_msg:
                    flash("This house number is already registered in this ward.", "error")
                else:
                    flash("Database error during registration. Please try again.", "error")
                cursor.close()
                return redirect(url_for("register"))

            cursor.close()
            flash("Registration successful! Waiting for admin approval.", "success")
            return redirect(url_for("home"))
        cursor.execute("SELECT DISTINCT ward_no FROM areas ORDER BY ward_no ASC")
        wards = cursor.fetchall()
        cursor.close()
        return render_template("register.html", wards=wards)

    @app.route("/get_areas_by_ward/<int:ward_no>")
    def get_areas_by_ward(ward_no):
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT area_name FROM areas WHERE ward_no=%s ORDER BY area_name ASC", (ward_no,))
        areas = cursor.fetchall()
        cursor.close()
        return jsonify([a['area_name'] for a in areas])

    @app.route('/admin_dashboard')
    def admin_dashboard():
        if "admin_id" not in session:
            flash("Please login first.", "error")
            return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            auto_reassign_expired_requests(cursor)
            mysql.connection.commit()
        except:
            mysql.connection.rollback()
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT household_id, full_name, ward_no, area, house_no, phone, upi_id, aadhaar_photo, created_at FROM household_users WHERE registration_status='Pending' ORDER BY created_at ASC LIMIT 4")
        pending_households = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        pending_count = cursor.fetchone()['cnt']
        cursor.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Approved'")
        total_households = cursor.fetchone()['cnt']
        cursor.execute("SELECT COUNT(*) as cnt FROM workers")
        total_workers = cursor.fetchone()['cnt']
        cursor.execute("SELECT COUNT(*) as cnt FROM staff")
        total_staff = cursor.fetchone()['cnt']
        cursor.execute("SELECT COUNT(*) as cnt FROM waste_categories")
        total_categories = cursor.fetchone()['cnt']
        cursor.execute("SELECT COUNT(*) as cnt FROM redeem_requests WHERE status='Pending'")
        total_rewards = cursor.fetchone()['cnt']
        cursor.execute("SELECT COUNT(*) as cnt FROM pickup_requests WHERE status='Completed' AND completed_date=CURDATE()")
        collected_today = cursor.fetchone()['cnt']
        cursor.execute("SELECT COUNT(*) as cnt FROM pickup_requests WHERE status IN ('Not at Home','Not Home') AND DATE(updated_at)=CURDATE()")
        rejected_today = cursor.fetchone()['cnt']
        cursor.execute("SELECT COALESCE(SUM(weight_kg),0) as total_kg FROM reward_logs")
        total_wastes_collected = cursor.fetchone()['total_kg'] or 0
        cursor.execute("""
            SELECT base.ward_no,
                (SELECT COUNT(*) FROM workers w WHERE w.ward_no = base.ward_no) as worker_count,
                (SELECT COUNT(*) FROM household_users hu WHERE hu.ward_no = base.ward_no AND hu.registration_status='Pending') as pending,
                (SELECT COUNT(*) FROM household_users hu WHERE hu.ward_no = base.ward_no AND hu.registration_status='Approved') as total_houses,
                (SELECT COUNT(*) FROM household_users hu WHERE hu.ward_no = base.ward_no AND hu.registration_status='Approved') as approved,
                (SELECT COUNT(*) FROM pickup_requests pr JOIN household_users hu ON pr.household_id=hu.household_id WHERE hu.ward_no=base.ward_no AND pr.status='Completed' AND pr.completed_date=CURDATE()) as collected_today,
                (SELECT COUNT(*) FROM pickup_requests pr JOIN household_users hu ON pr.household_id=hu.household_id WHERE hu.ward_no=base.ward_no AND pr.status IN ('Not at Home','Not Home') AND DATE(pr.updated_at)=CURDATE()) as rejected_today,
                (SELECT COALESCE(SUM(c.weight_kg),0) FROM reward_logs c JOIN household_users hu ON c.household_id=hu.household_id WHERE hu.ward_no=base.ward_no AND DATE(c.created_at)=CURDATE()) as kg_today,
                (SELECT COUNT(*) FROM pickup_requests pr JOIN household_users hu ON pr.household_id=hu.household_id WHERE hu.ward_no=base.ward_no AND pr.status='Pending' AND pr.created_at < NOW() - INTERVAL 1 HOUR) as stuck_requests
            FROM (SELECT DISTINCT ward_no FROM household_users WHERE ward_no IS NOT NULL UNION SELECT DISTINCT ward_no FROM workers WHERE ward_no IS NOT NULL) base
            ORDER BY base.ward_no
        """)
        ward_ledger = cursor.fetchall()
        cursor.execute("""
            SELECT w.full_name, w.ward_no,
                (SELECT COUNT(*) FROM pickup_requests pr WHERE pr.worker_id=w.worker_id AND pr.status='Pending' AND DATE(pr.created_at)=CURDATE()) as pending,
                (SELECT COUNT(*) FROM pickup_requests pr WHERE pr.worker_id=w.worker_id AND pr.status='Assigned' AND (DATE(pr.created_at)=CURDATE() OR DATE(pr.updated_at)=CURDATE())) as assigned,
                (SELECT COUNT(*) FROM pickup_requests pr WHERE pr.worker_id=w.worker_id AND pr.status='Completed' AND pr.completed_date=CURDATE()) as completed,
                (SELECT COUNT(*) FROM pickup_requests pr WHERE pr.worker_id=w.worker_id AND pr.status IN ('Not at Home','Not Home') AND DATE(pr.updated_at)=CURDATE()) as rejected,
                (SELECT COALESCE(SUM(c.weight_kg),0) FROM reward_logs c WHERE c.worker_id=w.worker_id AND DATE(c.created_at)=CURDATE()) as kg_today,
                (SELECT MAX(pr.updated_at) FROM pickup_requests pr WHERE pr.worker_id=w.worker_id AND (DATE(pr.created_at)=CURDATE() OR DATE(pr.updated_at)=CURDATE())) as last_active,
                'Active' as status
            FROM workers w
            ORDER BY completed DESC, pending DESC
        """)
        worker_performance = cursor.fetchall()
        cursor.execute("SELECT area_name, ward_no FROM areas ORDER BY ward_no ASC, area_name ASC")
        areas_list = cursor.fetchall()
        all_todays_requests = get_todays_requests_data(cursor)
        todays_requests = all_todays_requests[:5]
        total_todays_requests_count = len(all_todays_requests)

        try:
            cursor.execute("""
                SELECT rl.id, rl.pickup_id, rl.created_at as reassigned_at,
                    hu.full_name as household_name, hu.house_no, hu.ward_no, hu.area, hu.phone,
                    w_old.full_name as old_worker_name, w_new.full_name as new_worker_name,
                    pr.status, pr.created_at as original_request_time
                FROM reassign_logs rl
                JOIN household_users hu ON hu.household_id=rl.household_id
                JOIN pickup_requests pr ON pr.id=rl.pickup_id
                LEFT JOIN workers w_old ON w_old.worker_id=rl.old_worker_id
                LEFT JOIN workers w_new ON w_new.worker_id=rl.new_worker_id
                WHERE DATE(rl.created_at)=CURDATE()
                ORDER BY rl.created_at DESC
            """)
            reassigned_requests = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) as cnt FROM reassign_logs WHERE DATE(created_at)=CURDATE()")
            reassigned_count = cursor.fetchone()['cnt']
        except Exception as e:
            print("reassigned fetch error", e)
            reassigned_requests = []
            reassigned_count = 0
        try:
            cursor.execute("SELECT festive_mode FROM system_config WHERE id=1")
            fm_row = cursor.fetchone()
            festive_mode = fm_row['festive_mode'] if fm_row else 0
        except:
            festive_mode = 0
        today_str = datetime.now().strftime("%a · %d %b %Y").upper()

        try:
            cursor.execute("""
                SELECT COALESCE(c.pickup_id, c.id) as group_pickup_id,
                       MAX(c.created_at) as created_at, hu.full_name as household_name, hu.house_no, hu.ward_no, hu.area, 
                       w.full_name as worker_name, GROUP_CONCAT(DISTINCT wc.category_name SEPARATOR ', ') as waste_type,
                       SUM(c.weight_kg) as weight_kg, SUM(c.points) as points
                FROM reward_logs c
                JOIN household_users hu ON hu.household_id = c.household_id
                LEFT JOIN workers w ON w.worker_id = c.worker_id
                LEFT JOIN waste_categories wc ON wc.category_id = c.category_id
                GROUP BY COALESCE(c.pickup_id, c.id), hu.household_id, hu.full_name, hu.house_no, hu.ward_no, hu.area, w.worker_id, w.full_name
                ORDER BY created_at DESC
            """)
            all_collected_wastes = cursor.fetchall()
        except Exception as e:
            print("all_collected_wastes error", e)
            all_collected_wastes = []

        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS raw_waste_usage_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    category_id INT,
                    recycled_id INT,
                    staff_id INT,
                    kg_used DECIMAL(10,2),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        except: pass

        try:
            cursor.execute("""
                SELECT wc.category_name,
                    GREATEST(0, COALESCE((SELECT SUM(weight_kg) FROM reward_logs WHERE category_id=wc.category_id),0) 
                             - COALESCE((SELECT SUM(kg_used) FROM raw_waste_usage_log WHERE category_id=wc.category_id),0)
                             - COALESCE((SELECT SUM(qty_kg) FROM sales_log WHERE type='disposal' AND (recycled_id IN (SELECT recycled_id FROM recycled_products WHERE category_id=wc.category_id) OR buyer_name=wc.category_name OR description LIKE CONCAT('%%',wc.category_name,'%%'))),0)) as total_kg
                FROM waste_categories wc
                HAVING total_kg > 0
                ORDER BY total_kg DESC
            """)
            waste_by_category = cursor.fetchall()
            if not waste_by_category:
                cursor.execute("""
                    SELECT wc.category_name,
                        GREATEST(0, COALESCE((SELECT SUM(weight_kg) FROM reward_logs WHERE category_id=wc.category_id),0) 
                                 - COALESCE((SELECT SUM(kg_used) FROM raw_waste_usage_log WHERE category_id=wc.category_id),0)
                                 - COALESCE((SELECT SUM(qty_kg) FROM sales_log WHERE type='disposal' AND (recycled_id IN (SELECT recycled_id FROM recycled_products WHERE category_id=wc.category_id) OR buyer_name=wc.category_name OR description LIKE CONCAT('%%',wc.category_name,'%%'))),0)) as total_kg
                    FROM waste_categories wc
                    ORDER BY total_kg DESC
                """)
                waste_by_category = cursor.fetchall()
        except Exception as e:
            print("waste_by_category error", e)
            waste_by_category = []

        try:
            cursor.execute("SELECT worker_id, full_name, ward_no, area FROM workers ORDER BY ward_no, full_name")
            workers_all = cursor.fetchall()
        except:
            workers_all = []

        cursor.close()
        return render_template("admin_dashboard.html", pending_households=pending_households, pending_count=pending_count, total_households=total_households, total_workers=total_workers, total_staff=total_staff, total_categories=total_categories, total_rewards=total_rewards, collected_today=collected_today, rejected_today=rejected_today, total_wastes_collected=total_wastes_collected, ward_ledger=ward_ledger, worker_performance=worker_performance, areas_list=areas_list, todays_requests=todays_requests, total_todays_requests_count=total_todays_requests_count, reassigned_requests=reassigned_requests, reassigned_count=reassigned_count, festive_mode=festive_mode,today_date=today_str, all_collected_wastes=all_collected_wastes, waste_by_category=waste_by_category, workers_all=workers_all)

    @app.route('/admin/todays_requests')
    def admin_todays_requests():
        if "admin_id" not in session:
            flash("Please login first.", "error")
            return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        todays_requests = get_todays_requests_data(cursor)
        cursor.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        pending_count = cursor.fetchone()['cnt']
        cursor.execute("SELECT worker_id, full_name, ward_no, area FROM workers ORDER BY ward_no, full_name")
        workers_all = cursor.fetchall()
        
        total_count = len(todays_requests)
        on_leave_count = len([r for r in todays_requests if r.get('is_worker_on_leave')])
        stuck_count = len([r for r in todays_requests if r.get('is_stuck_over_1hr')])
        pending_req_count = len([r for r in todays_requests if r.get('status') == 'Pending'])
        completed_req_count = len([r for r in todays_requests if r.get('status') == 'Completed'])

        cursor.close()
        return render_template("admin_todays_requests.html",
                               todays_requests=todays_requests,
                               pending_count=pending_count,
                               workers_all=workers_all,
                               total_count=total_count,
                               on_leave_count=on_leave_count,
                               stuck_count=stuck_count,
                               pending_req_count=pending_req_count,
                               completed_req_count=completed_req_count,
                               today_date=datetime.now().strftime("%d %B %Y"))

    @app.route("/admin/reassign_request/<int:pickup_id>", methods=["POST"])
    def admin_reassign_request(pickup_id):
        if "admin_id" not in session: return redirect(url_for("home"))
        worker_id = request.form.get("worker_id")
        if not worker_id:
            flash("Select a worker to reassign", "error")
            return redirect(request.referrer or url_for("admin_dashboard"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT worker_id, household_id FROM pickup_requests WHERE id=%s", (pickup_id,))
        pr = cur.fetchone()
        if pr:
            old_worker_id = pr['worker_id']
            cur.execute("SELECT full_name FROM workers WHERE worker_id=%s", (worker_id,))
            wk = cur.fetchone()
            wname = wk['full_name'] if wk else f"Worker #{worker_id}"
            
            cur.execute("UPDATE pickup_requests SET worker_id=%s, status='Pending', updated_at=NOW(), reassign_count=IFNULL(reassign_count,0)+1, is_reassigned=1 WHERE id=%s", (worker_id, pickup_id))
            try:
                cur.execute("INSERT INTO reassign_logs (pickup_id, household_id, old_worker_id, new_worker_id, reason) VALUES (%s,%s,%s,%s,%s)",
                            (pickup_id, pr['household_id'], old_worker_id, worker_id, f'Admin manual reassign to {wname}'))
            except: pass
            mysql.connection.commit()
            flash(f"✅ Pickup #{pickup_id} manually reassigned to {wname}!", "success")
        cur.close()
        return redirect(request.referrer or url_for("admin_dashboard"))

    @app.route('/admin_profile')
    def admin_profile():
        if 'admin_id' not in session:
            return redirect('/admin_login')
        
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT admin_id, full_name, email, password FROM admin WHERE admin_id=%s", (session['admin_id'],))
        admin = cur.fetchone()
        
        # for sidebar counts
        cur.execute("SELECT COUNT(*) as c FROM household_users WHERE registration_status='Pending'")
        pending_count = cur.fetchone()['c']

        return render_template("admin_profile.html", admin=admin, pending_count=pending_count)


    @app.route('/admin/update_profile', methods=['POST'])
    def admin_update_profile():
        try:
            data = request.get_json() or {}
            full_name = data.get('full_name', '').strip()
            email = data.get('email', '').strip()
            password = data.get('password', '')

            if len(full_name) < 2:
                return jsonify({"success": False, "message": "Full name must be at least 2 characters."})
            if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
                return jsonify({"success": False, "message": "Please enter a valid email address."})
            if len(password) < 6:
                return jsonify({"success": False, "message": "Password must be at least 6 characters long."})

            cur = mysql.connection.cursor()
            cur.execute(
                "UPDATE admin SET full_name=%s, email=%s, password=%s WHERE admin_id=%s",
                (full_name, email, password, session['admin_id'])
            )
            mysql.connection.commit()
            
            session['admin_name'] = full_name
            cur.close()
            return jsonify({"success": True, "message": "Profile updated successfully!"})
        except Exception as e:
            print("ERROR:", e)
            return jsonify({"success": False, "message": str(e)})

            
    @app.route('/admin/feedbacks')
    def admin_feedbacks():
        if 'admin_id' not in session:
            return redirect('/admin_login')
        
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        
        # All feedbacks with household name
        cur.execute("""
            SELECT f.*, h.full_name as household_name, h.email as household_email, h.phone, h.ward_no, h.area
            FROM feedbacks f
            JOIN household_users h ON f.household_id = h.household_id
            ORDER BY f.created_at DESC
        """)
        feedbacks = cur.fetchall()
        
        # counts
        cur.execute("SELECT COUNT(*) as c FROM feedbacks WHERE status='Pending' AND type!='Rating'")
        pending_count = cur.fetchone()['c']

        cur.execute("SELECT COUNT(*) as c FROM feedbacks WHERE status='Resolved' AND type!='Rating'")
        resolved_count = cur.fetchone()['c']
        
        cur.execute("SELECT COUNT(*) as c FROM feedbacks WHERE type='Complaint'")
        complaint_count = cur.fetchone()['c']

        cur.execute("SELECT COUNT(*) as c FROM feedbacks WHERE type='Suggestion'")
        suggestion_count = cur.fetchone()['c']

        cur.execute("SELECT COUNT(*) as c FROM feedbacks WHERE type='Rating'")
        rating_count = cur.fetchone()['c']
        
        cur.execute("SELECT COUNT(*) as c FROM household_users WHERE registration_status='Pending'")
        household_pending = cur.fetchone()['c']

        # festive mode
        festive_mode = 0
        try:
            cur.execute("SELECT festive_mode FROM system_config WHERE id=1")
            cfg = cur.fetchone()
            if cfg:
                festive_mode = cfg.get('festive_mode', 0)
        except Exception as e:
            print("Config fetch error:", e)

        # reassigned leaves count for sidebar badge
        reassigned_count = 0
        try:
            cur.execute("SELECT COUNT(*) as c FROM worker_leaves WHERE leave_date = CURDATE()")
            l_row = cur.fetchone()
            if l_row: reassigned_count = l_row['c']
        except:
            reassigned_count = 0

        return render_template('admin_feedbacks.html', 
                            feedbacks=feedbacks, 
                            pending_count=household_pending,
                            complaint_count=complaint_count,
                            suggestion_count=suggestion_count,
                            rating_count=rating_count,
                            feedback_pending=pending_count,
                            resolved_count=resolved_count,
                            total_count=len(feedbacks),
                            festive_mode=festive_mode,
                            reassigned_count=reassigned_count)

    @app.route('/admin/feedback/update/<int:fid>', methods=['POST'])
    def update_feedback_status(fid):
        if 'admin_id' not in session:
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        
        data = request.get_json() or {}
        new_status = data.get('status')  # Resolved / Pending
        if new_status not in ['Pending', 'Resolved']:
            return jsonify({"success": False, "message": "Invalid status"})
        
        try:
            cur = mysql.connection.cursor()
            cur.execute("UPDATE feedbacks SET status=%s, admin_id=%s WHERE id=%s", 
                        (new_status, session['admin_id'], fid))
            mysql.connection.commit()
            return jsonify({"success": True, "message": f"Status updated to {new_status}"})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})


    @app.route("/admin/toggle_festive", methods=["GET","POST"])
    def toggle_festive():
        if "admin_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("INSERT INTO system_config (id, festive_mode) VALUES (1,1) ON DUPLICATE KEY UPDATE festive_mode=1")
        mysql.connection.commit()
        cur.close()
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/toggle_normal", methods=["GET","POST"])
    def toggle_normal():
        if "admin_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("INSERT INTO system_config (id, festive_mode) VALUES (1,0) ON DUPLICATE KEY UPDATE festive_mode=0")
        mysql.connection.commit()
        cur.close()
        return redirect(url_for("admin_dashboard"))

    @app.route("/approve_household/<int:household_id>", methods=["POST"])
    def approve_household(household_id):
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        area = request.form.get("area")
        if not area:
            flash("Please select area to allocate", "error")
            cursor.close()
            return redirect(url_for("pending_households"))
        cursor.execute("UPDATE household_users SET registration_status='Approved', area=%s WHERE household_id=%s", (area, household_id,))
        mysql.connection.commit()
        cursor.close()
        flash(f"Approved & allocated to {area}", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/reject_household/<int:household_id>")
    def reject_household(household_id):
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("UPDATE household_users SET registration_status='Rejected' WHERE household_id=%s", (household_id,))
        mysql.connection.commit()
        cursor.close()
        flash("Household rejected.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/view_aadhaar/<string:usertype>/<int:id>")
    def view_aadhaar(usertype, id):
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        filename = None
        if usertype == 'household':
            cursor.execute("SELECT aadhaar_photo FROM household_users WHERE household_id=%s", (id,))
            row = cursor.fetchone()
            if row: filename = row['aadhaar_photo']
        elif usertype == 'staff':
            cursor.execute("SELECT id_proof FROM staff WHERE staff_id=%s", (id,))
            row = cursor.fetchone()
            if row: filename = row['id_proof']
        elif usertype == 'worker':
            cursor.execute("SELECT id_proof FROM workers WHERE worker_id=%s", (id,))
            row = cursor.fetchone()
            if row: filename = row['id_proof']
        cursor.close()
        if not filename: return "File not found", 404
        return render_template("view_aadhaar.html", filename=filename)

    @app.route("/pending_households")
    def pending_households():
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT household_id, full_name, ward_no, house_no, phone, upi_id, aadhaar_photo, latitude, longitude, created_at FROM household_users WHERE registration_status='Pending' ORDER BY created_at ASC")
        pending = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        pending_count = cursor.fetchone()['cnt']
        cursor.execute("SELECT DISTINCT ward_no FROM areas ORDER BY ward_no ASC")
        all_wards = cursor.fetchall()
        cursor.execute("SELECT area_name, ward_no FROM areas ORDER BY ward_no ASC, area_name ASC")
        areas_list = cursor.fetchall()
        cursor.close()
        return render_template("pending_households.html", pending=pending, pending_count=pending_count, all_wards=all_wards, areas_list=areas_list)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    @app.route("/add_worker", methods=["GET", "POST"])
    def add_worker():
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip()
            phone = request.form.get("phone", "").strip()
            street = request.form.get("street", "").strip()
            city = request.form.get("city", "").strip()
            district = request.form.get("district", "").strip()
            pincode = request.form.get("pincode", "").strip()
            qualification = request.form.get("qualification", "").strip()
            area = request.form.get("area", "").strip()
            ward_no = request.form.get("ward_no", "").strip()
            password = request.form.get("password", "")
            file = request.files.get("id_proof")

            if len(full_name) < 2:
                flash("Full name must be at least 2 characters long.", "error")
                cursor.close()
                return redirect(url_for("add_worker"))
            if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
                flash("Please enter a valid email address.", "error")
                cursor.close()
                return redirect(url_for("add_worker"))
            if not re.match(r'^[6-9]\d{9}$', phone):
                flash("Please enter a valid 10-digit mobile number starting with 6-9.", "error")
                cursor.close()
                return redirect(url_for("add_worker"))
            if not re.match(r'^\d{6}$', pincode):
                flash("Please enter a valid 6-digit pincode.", "error")
                cursor.close()
                return redirect(url_for("add_worker"))
            if len(password) < 6:
                flash("Password must be at least 6 characters long.", "error")
                cursor.close()
                return redirect(url_for("add_worker"))
            if not ward_no or not area:
                flash("Please select Ward Number and Area.", "error")
                cursor.close()
                return redirect(url_for("add_worker"))
            if not file or not file.filename:
                flash("Please upload an ID proof document/photo.", "error")
                cursor.close()
                return redirect(url_for("add_worker"))
            allowed_exts = ('.jpg', '.jpeg', '.png', '.webp', '.pdf')
            if not file.filename.lower().endswith(allowed_exts):
                flash("ID proof file must be JPG, PNG, WEBP, or PDF format.", "error")
                cursor.close()
                return redirect(url_for("add_worker"))

            id_proof_path = None
            filename = secure_filename(file.filename)
            upload_folder = os.path.join(app.root_path, 'static', 'uploads', 'workers')
            os.makedirs(upload_folder, exist_ok=True)
            file.save(os.path.join(upload_folder, filename))
            id_proof_path = f"uploads/workers/{filename}"

            cursor.execute("SELECT worker_id FROM workers WHERE REPLACE(LOWER(TRIM(area)), ' ', '') = REPLACE(LOWER(TRIM(%s)), ' ', '') AND ward_no=%s", (area, ward_no))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("SELECT DISTINCT ward_no FROM areas ORDER BY ward_no ASC")
                wards = cursor.fetchall()
                cursor.execute("SELECT area_name, ward_no FROM areas ORDER BY area_name")
                areas = cursor.fetchall()
                cursor.close()
                flash(f"Worker already exists for area '{area}'!", "error")
                return render_template("add_worker.html", areas=areas, wards=wards)
            # --- Cross-table uniqueness: phone must be unique across workers + staff + household_users ---
            cursor.execute(
                "SELECT worker_id FROM workers WHERE phone=%s UNION SELECT staff_id FROM staff WHERE phone=%s UNION SELECT household_id FROM household_users WHERE phone=%s",
                (phone, phone, phone)
            )
            if cursor.fetchone():
                flash(f"❌ Phone number {phone} is already registered to another worker, staff, or household user.", "error")
                cursor.close()
                return redirect(url_for("add_worker"))

            # --- Cross-table uniqueness: email must be unique across workers + staff + household_users ---
            cursor.execute(
                "SELECT worker_id FROM workers WHERE LOWER(email)=LOWER(%s) UNION SELECT staff_id FROM staff WHERE LOWER(email)=LOWER(%s) UNION SELECT household_id FROM household_users WHERE LOWER(email)=LOWER(%s)",
                (email, email, email)
            )
            if cursor.fetchone():
                flash(f"❌ Email '{email}' is already registered to another worker, staff, or household user.", "error")
                cursor.close()
                return redirect(url_for("add_worker"))

            cursor.execute("INSERT INTO workers (full_name, email, phone, street, city, district, pincode, qualification, id_proof, area, ward_no, password) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (full_name, email, phone, street, city, district, pincode, qualification, id_proof_path, area, ward_no, password))
            mysql.connection.commit()
            cursor.close()
            flash("Worker added successfully!", "success")
            return redirect(url_for("add_worker"))
        cursor.execute("SELECT DISTINCT ward_no FROM areas ORDER BY ward_no ASC")
        wards = cursor.fetchall()
        cursor.execute("SELECT area_name, ward_no FROM areas ORDER BY area_name")
        areas = cursor.fetchall()
        cursor.close()
        return render_template("add_worker.html", areas=areas, wards=wards)

    @app.route("/view_workers")
    def view_workers():
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT worker_id, full_name, email, phone, street, city, district, pincode, ward_no, area, id_proof, qualification FROM workers ORDER BY worker_id ASC")
        workers = cursor.fetchall()
        cursor.execute("SELECT DISTINCT ward_no FROM areas ORDER BY ward_no ASC")
        all_wards = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        pending_count = cursor.fetchone()['cnt']
        cursor.close()
        return render_template("view_workers.html", workers=workers, all_wards=all_wards, pending_count=pending_count)

    @app.route("/edit_worker/<int:worker_id>", methods=["GET", "POST"])
    def edit_worker(worker_id):
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip()
            phone = request.form.get("phone", "").strip()
            street = request.form.get("street", "").strip()
            city = request.form.get("city", "").strip()
            district = request.form.get("district", "").strip()
            pincode = request.form.get("pincode", "").strip()
            area = request.form.get("area", "").strip()
            ward_no = request.form.get("ward_no", "").strip()
            password = request.form.get("password", "")
            qualification = request.form.get("qualification", "").strip()
            file = request.files.get("id_proof")

            if len(full_name) < 2:
                flash("Full name must be at least 2 characters long.", "error")
                cursor.close()
                return redirect(url_for("edit_worker", worker_id=worker_id))
            if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
                flash("Please enter a valid email address.", "error")
                cursor.close()
                return redirect(url_for("edit_worker", worker_id=worker_id))
            if not re.match(r'^[6-9]\d{9}$', phone):
                flash("Please enter a valid 10-digit mobile number starting with 6-9.", "error")
                cursor.close()
                return redirect(url_for("edit_worker", worker_id=worker_id))
            if not re.match(r'^\d{6}$', pincode):
                flash("Please enter a valid 6-digit pincode.", "error")
                cursor.close()
                return redirect(url_for("edit_worker", worker_id=worker_id))

            if file and file.filename:
                allowed_exts = ('.jpg', '.jpeg', '.png', '.webp', '.pdf')
                if not file.filename.lower().endswith(allowed_exts):
                    flash("ID proof file must be JPG, PNG, WEBP, or PDF format.", "error")
                    cursor.close()
                    return redirect(url_for("edit_worker", worker_id=worker_id))
                filename = secure_filename(file.filename)
                upload_folder = os.path.join(app.root_path, 'static', 'uploads', 'workers')
                os.makedirs(upload_folder, exist_ok=True)
                file.save(os.path.join(upload_folder, filename))
                id_proof_path = f"uploads/workers/{filename}"
                cursor.execute("UPDATE workers SET id_proof=%s WHERE worker_id=%s", (id_proof_path, worker_id))

            cursor.execute("SELECT worker_id FROM workers WHERE area=%s AND ward_no=%s AND worker_id!=%s", (area, ward_no, worker_id))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("SELECT area_name, ward_no FROM areas ORDER BY area_name")
                areas = cursor.fetchall()
                cursor.execute("SELECT * FROM workers WHERE worker_id = %s", (worker_id,))
                worker = cursor.fetchone()
                cursor.close()
                flash(f"Worker already exists for area '{area}'!", "error")
                return render_template("edit_worker.html", worker=worker, areas=areas)
            # --- Cross-table uniqueness: phone ---
            cursor.execute(
                "SELECT worker_id FROM workers WHERE phone=%s AND worker_id!=%s UNION SELECT staff_id FROM staff WHERE phone=%s UNION SELECT household_id FROM household_users WHERE phone=%s",
                (phone, worker_id, phone, phone)
            )
            if cursor.fetchone():
                flash(f"❌ Phone number {phone} is already registered to another worker, staff, or household user.", "error")
                cursor.close()
                return redirect(url_for("edit_worker", worker_id=worker_id))

            # --- Cross-table uniqueness: email ---
            cursor.execute(
                "SELECT worker_id FROM workers WHERE LOWER(email)=LOWER(%s) AND worker_id!=%s UNION SELECT staff_id FROM staff WHERE LOWER(email)=LOWER(%s) UNION SELECT household_id FROM household_users WHERE LOWER(email)=LOWER(%s)",
                (email, worker_id, email, email)
            )
            if cursor.fetchone():
                flash(f"❌ Email '{email}' is already registered to another worker, staff, or household user.", "error")
                cursor.close()
                return redirect(url_for("edit_worker", worker_id=worker_id))

            cursor.execute("UPDATE workers SET full_name=%s, email=%s, phone=%s, street=%s, city=%s, district=%s, pincode=%s, area=%s, ward_no=%s, password=%s, qualification=%s WHERE worker_id=%s", (full_name, email, phone, street, city, district, pincode, area, ward_no, password, qualification, worker_id))
            mysql.connection.commit()
            cursor.close()
            flash("Worker updated successfully!", "success")
            return redirect(url_for("view_workers"))
        cursor.execute("SELECT * FROM workers WHERE worker_id = %s", (worker_id,))
        worker = cursor.fetchone()
        cursor.execute("SELECT area_name, ward_no FROM areas ORDER BY area_name")
        areas = cursor.fetchall()
        cursor.close()
        return render_template("edit_worker.html", worker=worker, areas=areas)

    @app.route("/delete_worker/<int:worker_id>")
    def delete_worker(worker_id):
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            cursor.execute("UPDATE pickup_requests SET worker_id=NULL WHERE worker_id=%s", (worker_id,))
            cursor.execute("DELETE FROM workers WHERE worker_id=%s", (worker_id,))
            mysql.connection.commit()
            flash("Worker deleted successfully!", "success")
        except Exception as e:
            mysql.connection.rollback()
            flash(f"Cannot delete worker - assigned to pickups. Error: {str(e)}", "error")
        finally:
            cursor.close()
        return redirect(url_for("view_workers"))

    @app.route("/add_area", methods=["GET", "POST"])
    def add_area():
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        if request.method == "POST":
            area_name = request.form.get("area_name", "").strip()
            ward_no = request.form.get("ward_no", "").strip()
            if len(area_name) < 2:
                flash("Area name must be at least 2 characters long.", "error")
                cursor.close()
                return redirect(url_for("add_area"))
            try:
                w_num = int(ward_no)
                if w_num <= 0: raise ValueError()
            except:
                flash("Please enter a valid positive Ward Number.", "error")
                cursor.close()
                return redirect(url_for("add_area"))
            cursor.execute("SELECT area_id FROM areas WHERE LOWER(TRIM(area_name))=%s AND ward_no=%s", (area_name.lower(), ward_no))
            if cursor.fetchone():
                flash(f"Area '{area_name}' already exists in Ward {ward_no}!", "warning")
                cursor.close()
                return redirect(url_for("add_area"))
            cursor.execute("INSERT INTO areas (area_name, ward_no) VALUES (%s, %s)", (area_name, ward_no))
            mysql.connection.commit()
            flash(f"Area '{area_name}' added to Ward {ward_no}!", "success")
            cursor.close()
            return redirect(url_for("add_area"))
        selected_ward = request.args.get("ward", "all")
        if selected_ward!= "all":
            cursor.execute("SELECT area_id, area_name, ward_no FROM areas WHERE ward_no=%s ORDER BY area_name ASC", (selected_ward,))
        else:
            cursor.execute("SELECT area_id, area_name, ward_no FROM areas ORDER BY ward_no ASC, area_name ASC")
        areas = cursor.fetchall()
        cursor.execute("SELECT DISTINCT ward_no FROM areas ORDER BY ward_no ASC")
        all_wards = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        pending_count = cursor.fetchone()['cnt']
        cursor.close()
        return render_template("add_area.html", areas=areas, all_wards=all_wards, selected_ward=selected_ward, pending_count=pending_count)

    @app.route("/edit_area/<int:area_id>", methods=["GET","POST"])
    def edit_area(area_id):
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        if request.method == "POST":
            area_name = request.form.get("area_name", "").strip()
            ward_no = request.form.get("ward_no", "").strip()
            if len(area_name) < 2:
                flash("Area name must be at least 2 characters long.", "error")
                cursor.close()
                return redirect(url_for("edit_area", area_id=area_id))
            try:
                w_num = int(ward_no)
                if w_num <= 0: raise ValueError()
            except:
                flash("Please enter a valid positive Ward Number.", "error")
                cursor.close()
                return redirect(url_for("edit_area", area_id=area_id))

            # --- ADDED: Check duplicate before update ---
            cursor.execute("SELECT area_id FROM areas WHERE LOWER(TRIM(area_name))=%s AND ward_no=%s AND area_id!=%s", (area_name.lower().strip(), ward_no, area_id))
            if cursor.fetchone():
                flash(f"Area '{area_name}' already exists in Ward {ward_no}!", "warning")
                cursor.close()
                return redirect(url_for("edit_area", area_id=area_id))

            # 1. Fetch old area details
            cursor.execute("SELECT area_name, ward_no FROM areas WHERE area_id=%s", (area_id,))
            old_area = cursor.fetchone()

            if old_area:
                old_name = old_area['area_name']
                old_ward = old_area['ward_no']

                try: # --- ADDED: try/except for IntegrityError ---
                    # 2. Update areas table
                    cursor.execute("UPDATE areas SET area_name=%s, ward_no=%s WHERE area_id=%s", (area_name, ward_no, area_id))

                    # 3. Update corresponding household_users
                    cursor.execute("""
                        UPDATE household_users 
                        SET area=%s, ward_no=%s 
                        WHERE (REPLACE(LOWER(TRIM(area)), ' ', '') = REPLACE(LOWER(TRIM(%s)), ' ', '') OR area=%s) 
                        AND ward_no=%s
                    """, (area_name, ward_no, old_name, old_name, old_ward))

                    # 4. Update corresponding workers
                    cursor.execute("""
                        UPDATE workers 
                        SET area=%s, ward_no=%s 
                        WHERE (REPLACE(LOWER(TRIM(area)), ' ', '') = REPLACE(LOWER(TRIM(%s)), ' ', '') OR area=%s) 
                        AND ward_no=%s
                    """, (area_name, ward_no, old_name, old_name, old_ward))

                    # 5. Update corresponding worker_leaves if applicable
                    try:
                        cursor.execute("""
                            UPDATE worker_leaves 
                            SET area=%s, ward_no=%s 
                            WHERE (REPLACE(LOWER(TRIM(area)), ' ', '') = REPLACE(LOWER(TRIM(%s)), ' ', '') OR area=%s) 
                            AND ward_no=%s
                        """, (area_name, ward_no, old_name, old_name, old_ward))
                    except Exception as e:
                        print("worker_leaves update note:", e)

                    mysql.connection.commit()
                    flash("Area, Households, and Workers updated successfully!", "success")

                except Exception as e:
                    mysql.connection.rollback()
                    if "Duplicate" in str(e) or "unique_area_ward" in str(e):
                        flash(f"Area '{area_name}' already exists in Ward {ward_no}! Cannot duplicate.", "warning")
                    else:
                        flash(f"Error: {e}", "error")
                    cursor.close()
                    return redirect(url_for("edit_area", area_id=area_id))

            else:
                flash("Area not found.", "error")

            return redirect(url_for("add_area"))

        cursor.execute("SELECT * FROM areas WHERE area_id=%s", (area_id,))
        area = cursor.fetchone()
        cursor.close()
        return render_template("edit_area.html", area=area)



    @app.route("/delete_area/<int:area_id>")
    def delete_area(area_id):
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("DELETE FROM areas WHERE area_id=%s",(area_id,))
        mysql.connection.commit()
        cursor.close()
        flash("Area deleted successfully.","success")
        return redirect(url_for("add_area"))

    @app.route("/view_households")
    def view_households():
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT hu.household_id, hu.full_name, hu.email, hu.phone, hu.area, hu.ward_no, hu.house_no, hu.upi_id, hu.aadhaar_photo, hu.latitude, hu.longitude, hu.reward_wallet FROM household_users hu WHERE hu.registration_status='Approved' ORDER BY hu.household_id DESC")
        households = cursor.fetchall()
        cursor.execute("SELECT area_name FROM areas ORDER BY area_name ASC")
        areas = cursor.fetchall()
        cursor.execute("SELECT DISTINCT ward_no FROM areas ORDER BY ward_no ASC")
        all_wards = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        pending_count = cursor.fetchone()['cnt']
        cursor.close()
        return render_template("view_households.html", households=households, areas=areas, all_wards=all_wards, pending_count=pending_count)

    @app.route("/api/households/<int:household_id>")
    def get_household(household_id):
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT household_id, full_name, email, phone, area, ward_no, house_no, aadhaar_photo, latitude, longitude FROM household_users WHERE household_id=%s",(household_id,))
        row = cursor.fetchone()
        cursor.close()
        if not row: return jsonify({"success":False, "message":"Household not found."})
        return jsonify({"success":True, "household":{"id": row['household_id'], "full_name": row['full_name'], "email": row['email'], "phone": row['phone'], "area": row['area'], "ward_no": row['ward_no'], "house_no": row['house_no'], "aadhaar_photo": row['aadhaar_photo'], "lat": row['latitude'], "lng": row['longitude']}})

    @app.route("/api/households/<int:household_id>", methods=["PUT"])
    def update_household(household_id):
        data = request.get_json() or {}
        full_name = data.get("full_name", "").strip()
        ward_no = data.get("ward_no")
        area = data.get("area", "").strip()
        house_no = data.get("house_no", "").strip()

        if len(full_name) < 2:
            return jsonify({"success": False, "message": "Name must contain at least 2 characters."})
        if not house_no:
            return jsonify({"success": False, "message": "House number is required."})

        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        # Uniqueness: house_no must not belong to another household
        cursor.execute(
            "SELECT household_id FROM household_users WHERE UPPER(TRIM(house_no))=UPPER(TRIM(%s)) AND household_id!=%s",
            (house_no, household_id)
        )
        if cursor.fetchone():
            cursor.close()
            return jsonify({"success": False, "message": f"❌ House number '{house_no}' is already registered to another household."})

        if ward_no and area:
            cursor.execute("UPDATE household_users SET full_name=%s, ward_no=%s, area=%s, house_no=%s WHERE household_id=%s", (full_name, ward_no, area, house_no, household_id))
        else:
            cursor.execute("UPDATE household_users SET full_name=%s, house_no=%s WHERE household_id=%s", (full_name, house_no, household_id))
        mysql.connection.commit()
        cursor.close()
        return jsonify({"success": True, "message": "Household details updated successfully."})

    @app.route("/api/households/<int:id>", methods=["DELETE"])
    def delete_household(id):
        cur = mysql.connection.cursor()
        try:
            cur.execute("DELETE FROM reward_logs WHERE household_id=%s", (id,))
            cur.execute("DELETE FROM redeem_requests WHERE household_id=%s", (id,))
            cur.execute("DELETE FROM pickup_requests WHERE household_id=%s", (id,))
            cur.execute("DELETE FROM household_users WHERE household_id=%s", (id,))
            mysql.connection.commit()
            cur.close()
            return jsonify({"success": True})
        except Exception as e:
            mysql.connection.rollback()
            cur.close()
            return jsonify({"success": False, "error": str(e)}), 400

    @app.route("/add_staff", methods=["GET", "POST"])
    def add_staff():
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip()
            phone = request.form.get("phone", "").strip()
            street = request.form.get("street", "").strip()
            city = request.form.get("city", "").strip()
            district = request.form.get("district", "").strip()
            pincode = request.form.get("pincode", "").strip()
            qualification = request.form.get("qualification", "").strip()
            password = request.form.get("password", "")
            file = request.files.get("id_proof")

            if len(full_name) < 2:
                flash("Full name must be at least 2 characters long.", "error")
                cursor.close()
                return redirect(url_for("add_staff"))
            if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
                flash("Please enter a valid email address.", "error")
                cursor.close()
                return redirect(url_for("add_staff"))
            if not re.match(r'^[6-9]\d{9}$', phone):
                flash("Please enter a valid 10-digit mobile number starting with 6-9.", "error")
                cursor.close()
                return redirect(url_for("add_staff"))
            if not re.match(r'^\d{6}$', pincode):
                flash("Please enter a valid 6-digit pincode.", "error")
                cursor.close()
                return redirect(url_for("add_staff"))
            if len(password) < 6:
                flash("Password must be at least 6 characters long.", "error")
                cursor.close()
                return redirect(url_for("add_staff"))
            if not file or not file.filename:
                flash("Please upload ID proof document/photo.", "error")
                cursor.close()
                return redirect(url_for("add_staff"))
            allowed_exts = ('.jpg', '.jpeg', '.png', '.webp', '.pdf')
            if not file.filename.lower().endswith(allowed_exts):
                flash("ID proof file must be JPG, PNG, WEBP, or PDF format.", "error")
                cursor.close()
                return redirect(url_for("add_staff"))

            # --- Cross-table uniqueness: phone ---
            cursor.execute(
                "SELECT staff_id FROM staff WHERE phone=%s UNION SELECT worker_id FROM workers WHERE phone=%s UNION SELECT household_id FROM household_users WHERE phone=%s",
                (phone, phone, phone)
            )
            if cursor.fetchone():
                flash(f"❌ Phone number {phone} is already registered to another staff, worker, or household user.", "error")
                cursor.close()
                return redirect(url_for("add_staff"))

            # --- Cross-table uniqueness: email ---
            cursor.execute(
                "SELECT staff_id FROM staff WHERE LOWER(email)=LOWER(%s) UNION SELECT worker_id FROM workers WHERE LOWER(email)=LOWER(%s) UNION SELECT household_id FROM household_users WHERE LOWER(email)=LOWER(%s)",
                (email, email, email)
            )
            if cursor.fetchone():
                flash(f"❌ Email '{email}' is already registered to another staff, worker, or household user.", "error")
                cursor.close()
                return redirect(url_for("add_staff"))

            filename = secure_filename(file.filename)
            upload_folder = os.path.join(app.root_path, 'static', 'uploads', 'staff')
            os.makedirs(upload_folder, exist_ok=True)
            file.save(os.path.join(upload_folder, filename))
            id_proof_path = f"uploads/staff/{filename}"

            try:
                cursor.execute("INSERT INTO staff (full_name, email, phone, street, city, district, pincode, qualification, id_proof, password) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                               (full_name, email, phone, street, city, district, pincode, qualification, id_proof_path, password))
                mysql.connection.commit()
                flash("Staff added successfully!", "success")
            except Exception as e:
                print("add_staff error", e)
                mysql.connection.rollback()
                flash(f"Error: {e}", "error")
            cursor.close()
            return redirect(url_for('add_staff'))
        cursor.close()
        return render_template("add_staff.html")

    @app.route("/view_staff")
    def view_staff():
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT staff_id, full_name, email, phone, street, city, district, pincode, id_proof, qualification, created_at FROM staff ORDER BY staff_id ASC")
        staffs = cursor.fetchall()

        cursor.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        row = cursor.fetchone()
        if row is None:
            pending_count = 0
        elif isinstance(row, dict):
            pending_count = row.get('cnt', 0)
        else: # tuple cursor
            pending_count = row[0] if len(row) > 0 else 0

        cursor.close()
        return render_template("view_staff.html", staffs=staffs, pending_count=pending_count)


    @app.route("/edit_staff/<int:staff_id>", methods=["GET", "POST"])
    def edit_staff(staff_id):
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip()
            phone = request.form.get("phone", "").strip()
            street = request.form.get("street", "").strip()
            city = request.form.get("city", "").strip()
            district = request.form.get("district", "").strip()
            pincode = request.form.get("pincode", "").strip()
            qualification = request.form.get("qualification", "").strip()

            password = request.form.get("password", "").strip()
            if len(password) < 6:
                flash("Password must be at least 6 characters long.", "error")
                cursor.close()
                return redirect(url_for("edit_staff", staff_id=staff_id))

            if len(full_name) < 2:
                flash("Full name must be at least 2 characters long.", "error")
                cursor.close()
                return redirect(url_for("edit_staff", staff_id=staff_id))
            if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
                flash("Please enter a valid email address.", "error")
                cursor.close()
                return redirect(url_for("edit_staff", staff_id=staff_id))
            if not re.match(r'^[6-9]\d{9}$', phone):
                flash("Please enter a valid 10-digit mobile number starting with 6-9.", "error")
                cursor.close()
                return redirect(url_for("edit_staff", staff_id=staff_id))
            if not re.match(r'^\d{6}$', pincode):
                flash("Please enter a valid 6-digit pincode.", "error")
                cursor.close()
                return redirect(url_for("edit_staff", staff_id=staff_id))

            cursor.execute(
                "SELECT staff_id FROM staff WHERE phone=%s AND staff_id!=%s UNION SELECT worker_id FROM workers WHERE phone=%s UNION SELECT household_id FROM household_users WHERE phone=%s",
                (phone, staff_id, phone, phone)
            )
            if cursor.fetchone():
                flash(f"❌ Phone number {phone} is already registered to another staff, worker, or household user.", "error")
                cursor.close()
                return redirect(url_for("edit_staff", staff_id=staff_id))

            cursor.execute(
                "SELECT staff_id FROM staff WHERE LOWER(email)=LOWER(%s) AND staff_id!=%s UNION SELECT worker_id FROM workers WHERE LOWER(email)=LOWER(%s) UNION SELECT household_id FROM household_users WHERE LOWER(email)=LOWER(%s)",
                (email, staff_id, email, email)
            )
            if cursor.fetchone():
                flash(f"❌ Email '{email}' is already registered to another staff, worker, or household user.", "error")
                cursor.close()
                return redirect(url_for("edit_staff", staff_id=staff_id))

            cursor.execute("UPDATE staff SET full_name=%s, email=%s, phone=%s, street=%s, city=%s, district=%s, pincode=%s, qualification=%s, password=%s WHERE staff_id=%s", (full_name, email, phone, street, city, district, pincode, qualification, password, staff_id))        
            mysql.connection.commit()
            cursor.close()
            flash("Staff updated!", "success")
            return redirect(url_for("view_staff"))

        # --- FIX HERE: Convert dict to tuple for your old HTML ---
        cursor.execute("SELECT * FROM staff WHERE staff_id=%s", (staff_id,))
        staff_dict = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        pending_count = cursor.fetchone()['cnt']
        cursor.close()

        # Convert dict to tuple so staff[0], staff[1] works in your HTML
        if staff_dict:
            staff = (
                staff_dict['staff_id'], # 0
                staff_dict['full_name'], # 1
                staff_dict['email'], # 2
                staff_dict['phone'], # 3
                staff_dict['street'], # 4
                staff_dict['city'], # 5
                staff_dict['district'], # 6
                staff_dict['pincode'], # 7
                staff_dict['id_proof'], # 8
                staff_dict['qualification'], # 9
                staff_dict['password'] # 10
            )
        else:
            staff = None

        return render_template("edit_staff.html", staff=staff, pending_count=pending_count)

    @app.route("/delete_staff/<int:staff_id>")
    def delete_staff(staff_id):
        if "admin_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("DELETE FROM staff WHERE staff_id=%s", (staff_id,))
        mysql.connection.commit()
        cursor.close()
        flash("Staff deleted!", "success")
        return redirect(url_for("view_staff"))

    @app.route("/wastes_rewards", methods=["GET","POST"])
    def wastes_rewards():
        if "admin_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        if request.method == "POST":
            cat_name = request.form.get("category_name", "").strip()
            if len(cat_name) < 2:
                flash("Category name must be at least 2 characters long.", "warning")
                cur.close()
                return redirect(url_for("wastes_rewards"))
            try:
                pts = int(request.form.get("points_per_kg", 0))
                if pts < 0: raise ValueError()
            except:
                flash("Points per KG must be a non-negative integer.", "warning")
                cur.close()
                return redirect(url_for("wastes_rewards"))
            try:
                cur.execute("INSERT INTO waste_categories (category_name, points_per_kg, is_disposal) VALUES (%s,%s,%s)", (cat_name, pts, int(request.form.get("is_disposal",0))))
                mysql.connection.commit()
                flash("Category added successfully", "success")
            except Exception as e:
                print(e)
                flash("Category already exists!", "warning")
            cur.close()
            return redirect(url_for("wastes_rewards"))
        edit_cat = None
        if request.args.get("edit"):
            cur.execute("SELECT category_id, category_name, points_per_kg, is_disposal FROM waste_categories WHERE category_id=%s", (request.args.get("edit"),))
            edit_cat = cur.fetchone()
        cur.execute("SELECT category_id, category_name, points_per_kg, is_disposal FROM waste_categories ORDER BY category_id asc")
        cats = cur.fetchall()
        cur.close()
        return render_template("wastes_rewards.html", cats=cats, edit_cat=edit_cat)

    @app.route("/wastes_rewards/edit/<int:cid>", methods=["POST"])
    def edit_wastes_rewards(cid):
        if "admin_id" not in session: return redirect(url_for("home"))
        cat_name = request.form.get("category_name", "").strip()
        if len(cat_name) < 2:
            flash("Category name must be at least 2 characters long.", "warning")
            return redirect(url_for("wastes_rewards"))
        try:
            pts = int(request.form.get("points_per_kg", 0))
            if pts < 0: raise ValueError()
        except:
            flash("Points per KG must be a non-negative integer.", "warning")
            return redirect(url_for("wastes_rewards"))
        cur = mysql.connection.cursor()
        cur.execute("UPDATE waste_categories SET category_name=%s, points_per_kg=%s, is_disposal=%s WHERE category_id=%s", (cat_name, pts, int(request.form.get("is_disposal",0)), cid))
        mysql.connection.commit(); cur.close()
        flash("Category updated!", "success")
        return redirect(url_for("wastes_rewards"))

    @app.route("/wastes_rewards/delete/<int:cid>")
    def delete_wastes_rewards(cid):
        if "admin_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor()
        cur.execute("DELETE FROM waste_categories WHERE category_id=%s", (cid,))
        mysql.connection.commit(); cur.close()
        flash("Deleted category", "warning")
        return redirect(url_for("wastes_rewards"))

    @app.route('/admin/viewsales')
    def admin_viewsales():
        if "admin_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        filter_type = request.args.get('type', 'resale')
        filter_date = request.args.get('date', 'all')
        search = request.args.get('search', '').strip()
        query = """
            SELECT s.id, s.recycled_id, s.staff_id, s.type, s.qty_kg, s.qty_count,
                   s.rate_per_kg, s.rate_per_count, s.amount, s.buyer_name, s.buyer_company,
                   s.buyer_contact, s.description, s.created_at,
                   s.payment_mode, s.vehicle_no, s.gst_percent,
                   COALESCE(rp.recycled_name, s.buyer_name, 'Disposal / Raw Waste') as recycled_name,
                   COALESCE(rp.unit, 'kg') as unit,
                   COALESCE(rp.resale_price_per_kg, 0) as resale_price_per_kg,
                   st.full_name as staff_name
            FROM sales_log s
            LEFT JOIN recycled_products rp ON rp.recycled_id = s.recycled_id
            LEFT JOIN staff st ON st.staff_id = s.staff_id
            WHERE 1=1
        """
        params = []
        if filter_type != 'all':
            query += " AND s.type=%s"
            params.append(filter_type)
        if filter_date == 'today':
            query += " AND DATE(s.created_at)=CURDATE()"
        elif filter_date == 'week':
            query += " AND s.created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
        elif filter_date == 'month':
            query += " AND MONTH(s.created_at)=MONTH(CURDATE()) AND YEAR(s.created_at)=YEAR(CURDATE())"
        if search:
            query += " AND (rp.recycled_name LIKE %s OR s.buyer_company LIKE %s OR s.buyer_name LIKE %s OR st.full_name LIKE %s OR s.buyer_contact LIKE %s OR s.vehicle_no LIKE %s OR s.payment_mode LIKE %s)"
            like = f"%{search}%"
            params.extend([like, like, like, like, like, like, like])
        query += " ORDER BY s.created_at DESC LIMIT 500"
        cur.execute(query, params)
        sales = cur.fetchall()
        cur.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        pending_count = cur.fetchone()['cnt']
        cur.execute("SELECT COALESCE(SUM(amount),0) as total, COALESCE(SUM(qty_kg),0) as kg, COUNT(*) as cnt FROM sales_log WHERE type='resale'")
        total_stats = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(amount),0) as today_rev, COALESCE(SUM(qty_kg),0) as today_kg FROM sales_log WHERE type='resale' AND DATE(created_at)=CURDATE()")
        today_stats = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(amount),0) as month_rev, COUNT(*) as month_cnt FROM sales_log WHERE type='resale' AND MONTH(created_at)=MONTH(CURDATE()) AND YEAR(created_at)=YEAR(CURDATE())")
        month_stats = cur.fetchone()
        cur.close()
        return render_template('admin_viewsales.html',
                               sales=sales, pending_count=pending_count,
                               total_rev=total_stats['total'], total_kg=total_stats['kg'], total_cnt=total_stats['cnt'],
                               today_rev=today_stats['today_rev'], today_kg=today_stats['today_kg'],
                               month_rev=month_stats['month_rev'], month_cnt=month_stats['month_cnt'],
                               filter_type=filter_type, filter_date=filter_date, search=search)

    @app.route("/admin/recycled_products", methods=["GET"])
    def recycled_products():
        if "admin_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT category_id, category_name, is_disposal FROM waste_categories WHERE is_disposal=0 OR is_disposal IS NULL ORDER BY category_name")
        cats = cur.fetchall()
        cur.execute("""
            SELECT rp.recycled_id, rp.recycled_name, rp.category_id, rp.unit, rp.resale_price_per_kg,
                   wc.category_name, COALESCE(i.stock_kg,0) as stock_kg
            FROM recycled_products rp
            JOIN waste_categories wc ON wc.category_id=rp.category_id
            LEFT JOIN inventory i ON i.recycled_id=rp.recycled_id
            ORDER BY rp.recycled_id DESC
        """)
        recycled_items = cur.fetchall()
        cur.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        pending_count = cur.fetchone()['cnt']
        cur.close()
        return render_template("recycled_products.html", cats=cats, recycled_items=recycled_items, pending_count=pending_count)

    @app.route("/admin/recycled_products/add", methods=["POST"])
    def add_recycled_product():
        if "admin_id" not in session:
            return redirect(url_for("home"))
        name = request.form.get("recycled_name","").strip()
        cat_id = request.form.get("category_id")
        unit = request.form.get("unit","kg").strip()
        price_str = request.form.get("resale_price_per_kg", "0")
        if len(name) < 2:
            flash("Recycled product name must be at least 2 characters long.", "error")
            return redirect(url_for("recycled_products"))
        if not cat_id:
            flash("Please select a valid waste category.", "error")
            return redirect(url_for("recycled_products"))
        try:
            price = float(price_str)
            if price < 0: raise ValueError()
        except:
            flash("Resale price must be a valid non-negative number.", "error")
            return redirect(url_for("recycled_products"))

        cur = mysql.connection.cursor()
        cur.execute("INSERT INTO recycled_products (recycled_name, category_id, resale_price_per_kg, unit) VALUES (%s,%s,%s,%s)",
                    (name, cat_id, price, unit))
        new_id = cur.lastrowid
        cur.execute("INSERT INTO inventory (recycled_id, stock_kg, unit, status) VALUES (%s,0,%s,'EMPTY')", (new_id, unit))
        mysql.connection.commit()
        cur.close()
        flash(f"Added '{name}'", "success")
        return redirect(url_for("recycled_products"))

    @app.route("/admin/recycled_products/edit/<int:rid>", methods=["POST"])
    def edit_recycled_product(rid):
        if "admin_id" not in session:
            return redirect(url_for("home"))
        name = request.form.get("recycled_name","").strip()
        cat_id = request.form.get("category_id")
        unit = request.form.get("unit","kg").strip()
        price_str = request.form.get("resale_price_per_kg", "0")
        if len(name) < 2:
            flash("Recycled product name must be at least 2 characters long.", "error")
            return redirect(url_for("recycled_products"))
        if not cat_id:
            flash("Please select a valid waste category.", "error")
            return redirect(url_for("recycled_products"))
        try:
            price = float(price_str)
            if price < 0: raise ValueError()
        except:
            flash("Resale price must be a valid non-negative number.", "error")
            return redirect(url_for("recycled_products"))

        cur = mysql.connection.cursor()
        cur.execute("UPDATE recycled_products SET recycled_name=%s, category_id=%s, unit=%s, resale_price_per_kg=%s WHERE recycled_id=%s",
                    (name, cat_id, unit, price, rid))
        try:
            cur.execute("UPDATE inventory SET unit=%s WHERE recycled_id=%s", (unit, rid))
        except:
            pass
        mysql.connection.commit()
        cur.close()
        flash(f"Updated ID {rid} - {name}", "success")
        return redirect(url_for("recycled_products"))

    @app.route("/admin/recycled_products/delete/<int:rid>", methods=["POST"])
    def delete_recycled_product(rid):
        if "admin_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor()
        cur.execute("DELETE FROM inventory WHERE recycled_id=%s", (rid,))
        cur.execute("DELETE FROM recycled_products WHERE recycled_id=%s", (rid,))
        mysql.connection.commit()
        cur.close()
        flash("Deleted", "success")
        return redirect(url_for("recycled_products"))

    @app.route("/admin/rewards_redeem")
    def admin_rewards_redeem():
        if "admin_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor()
        try:
            cur.execute("ALTER TABLE redeem_requests ADD COLUMN payment_screenshot VARCHAR(255) NULL AFTER txn_id")
            mysql.connection.commit()
        except:
            pass
        cur.execute("SELECT rr.id, hu.full_name, hu.house_no, hu.ward_no, hu.area, hu.phone, rr.points_redeemed, rr.rupees, rr.upi_id, rr.status, rr.created_at, rr.txn_id, rr.payment_screenshot FROM redeem_requests rr JOIN household_users hu ON hu.household_id = rr.household_id ORDER BY FIELD(rr.status,'Pending','Credited','Rejected'), rr.created_at DESC")
        requests = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM household_users WHERE registration_status='Pending'")
        pending_count = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(rupees),0) FROM redeem_requests WHERE status='Pending'")
        pending_amount = cur.fetchone()[0]
        cur.close()
        return render_template("admin_redeem_requests.html", requests=requests, pending_count=pending_count, pending_amount=pending_amount)

    @app.route('/admin/viewinventory')
    def admin_viewinventory():
        if "admin_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("""
            SELECT r.recycled_id, r.recycled_name, r.unit, r.resale_price_per_kg,
                   c.category_name, c.is_disposal,
                   COALESCE(i.stock_kg, 0)    as current_kg,
                   COALESCE(i.stock_count, 0) as current_cnt,
                   i.status,
                   COALESCE(
                       (SELECT SUM(CASE WHEN sl.qty_kg > 0 THEN sl.qty_kg ELSE sl.qty_count END)
                        FROM sales_log sl
                        WHERE sl.recycled_id = r.recycled_id AND sl.type = 'resale'), 0
                   ) as sold_qty
            FROM recycled_products r
            JOIN waste_categories c ON r.category_id = c.category_id
            LEFT JOIN inventory i ON i.recycled_id = r.recycled_id
            WHERE c.is_disposal = 0 OR c.is_disposal IS NULL
            ORDER BY r.recycled_name ASC
        """)
        raw_inventory = cur.fetchall()

        # Build enriched inventory list with correct remaining / stock_in / sold
        inventory = []
        for item in raw_inventory:
            unit = (item['unit'] or 'kg').lower()
            if unit == 'kg':
                remaining = float(item['current_kg'] or 0)
            else:
                remaining = int(item['current_cnt'] or 0)
            sold = float(item['sold_qty'] or 0)
            stock_in = remaining + sold          # total ever added = current + sold
            item['remaining_stock'] = remaining
            item['total_stock']     = round(stock_in, 2)
            item['sold_qty']        = round(sold, 2)
            inventory.append(item)

        cur.execute("SELECT category_id, category_name, is_disposal FROM waste_categories WHERE is_disposal=0 OR is_disposal IS NULL ORDER BY category_name")
        categories = cur.fetchall()
        cur.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        row = cur.fetchone()
        pending_count = row['cnt'] if row else 0
        cur.close()

        total_items     = len(inventory)
        total_stock     = sum([float(i['remaining_stock'] or 0) for i in inventory])
        total_value     = sum([float(i['remaining_stock'] or 0) * float(i['resale_price_per_kg'] or 0) for i in inventory])
        low_stock_count = len([i for i in inventory if 0 < float(i['remaining_stock'] or 0) < 10])

        return render_template('admin_viewinventory.html',
                               inventory=inventory,
                               categories=categories,
                               pending_count=pending_count,
                               total_items=total_items,
                               total_stock=int(total_stock),
                               total_value=int(total_value),
                               low_stock_count=low_stock_count)

    @app.route("/admin/redeem_credited/<int:req_id>", methods=["POST"])
    def admin_redeem_credited(req_id):
        if "admin_id" not in session: return redirect(url_for("home"))
        txn_id = request.form.get("txn_id","").strip()
        if not txn_id:
            flash("Enter Transaction ID", "error")
            return redirect(url_for("admin_rewards_redeem"))
            
        file_path = None
        if "payment_screenshot" in request.files:
            file = request.files["payment_screenshot"]
            if file and file.filename:
                allowed_extensions = ('.jpg', '.jpeg', '.png', '.webp', '.pdf')
                if file.filename.lower().endswith(allowed_extensions):
                    filename = secure_filename(file.filename)
                    save_name = f"redeem_{req_id}_{int(datetime.now().timestamp())}_{filename}"
                    save_path = os.path.join(app.config["PAYMENT_UPLOAD_FOLDER"], save_name)
                    file.save(save_path)
                    file_path = "uploads/payments/" + save_name

        cur = mysql.connection.cursor()
        try:
            cur.execute("ALTER TABLE redeem_requests ADD COLUMN payment_screenshot VARCHAR(255) NULL AFTER txn_id")
            mysql.connection.commit()
        except:
            pass

        if file_path:
            cur.execute("UPDATE redeem_requests SET status='Credited', txn_id=%s, payment_screenshot=%s WHERE id=%s", (txn_id, file_path, req_id))
        else:
            cur.execute("UPDATE redeem_requests SET status='Credited', txn_id=%s WHERE id=%s", (txn_id, req_id))
        mysql.connection.commit()
        cur.close()
        flash(f"Request #{req_id} marked Credited - {txn_id}", "success")
        return redirect(url_for("admin_rewards_redeem"))

    @app.route("/admin/redeem_rejected/<int:req_id>")
    def admin_redeem_rejected(req_id):
        if "admin_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor()
        cur.execute("SELECT household_id, points_redeemed FROM redeem_requests WHERE id=%s", (req_id,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE household_users SET reward_wallet = reward_wallet + %s WHERE household_id=%s", (row[1], row[0]))
            cur.execute("UPDATE redeem_requests SET status='Rejected' WHERE id=%s", (req_id,))
            mysql.connection.commit()
        cur.close()
        flash(f"Request #{req_id} rejected & points refunded", "warning")
        return redirect(url_for("admin_rewards_redeem"))

    @app.route("/household_dashboard")
    def household_dashboard():
        if "household_id" not in session: return redirect(url_for("login"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            auto_reassign_expired_requests(cursor)
            mysql.connection.commit()
        except:
            mysql.connection.rollback()
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT household_id, full_name, email, phone, area, ward_no, house_no, reward_wallet FROM household_users WHERE household_id = %s", (session["household_id"],))
        household = cursor.fetchone()
        pickup_status = 'No Request'
        active_pickup = None
        try:
            cursor.execute("SELECT id, status, created_at, completed_date FROM pickup_requests WHERE household_id=%s AND ((status IN ('Pending','Assigned') AND created_at >= NOW() - INTERVAL 24 HOUR) OR (status='Completed' AND completed_date = CURDATE())) ORDER BY id DESC LIMIT 1", (session["household_id"],))
            row = cursor.fetchone()
            if row:
                pickup_status = row['status']
                if row['status'] in ('Pending','Assigned'):
                    active_pickup = {"id": row['id'], "status": row['status'], "created_at": row['created_at']}
                elif row['status'] == 'Completed':
                    pickup_status = f"Completed Today ({row['completed_date']})"
        except: pickup_status = 'No Request'
        assigned_worker = None
        try:
            cursor.execute("SELECT full_name, phone FROM workers WHERE ward_no=%s AND REPLACE(LOWER(TRIM(area)), ' ', '') = REPLACE(LOWER(TRIM(%s)), ' ', '') LIMIT 1", (household['ward_no'], household['area']))
            assigned_worker = cursor.fetchone()
        except: assigned_worker = None
        points = {}
        categories = []
        try:
            cursor.execute("SELECT category_id, category_name, points_per_kg FROM waste_categories ORDER BY category_id ASC")
            categories = cursor.fetchall()
            for r in categories:
                points[r['category_name'].lower().strip()] = r['points_per_kg']
        except:
            points = {}
            categories = []
        completed_count = 0; total_weight = 0; co2_saved = 0; trees_eq = 0; progress_pct = 0
        try:
            cursor.execute("SELECT COUNT(*) as cnt FROM pickup_requests WHERE household_id=%s AND status='Completed'", (session["household_id"],))
            c_row = cursor.fetchone()
            completed_count = c_row['cnt'] if c_row else 0

            cursor.execute("SELECT COALESCE(SUM(weight_kg),0) as tw FROM reward_logs WHERE household_id=%s", (session["household_id"],))
            w_row = cursor.fetchone()
            total_weight = float(w_row['tw'] if w_row else 0)
            co2_saved = round(total_weight * 0.5, 2)
            trees_eq = round(co2_saved / 20, 2)
            progress_pct = int((completed_count / 100 * 100)) if completed_count < 100 else 100
        except: pass

        try:
            cursor.execute("DELETE FROM notifications WHERE household_id=%s AND (created_at < NOW() - INTERVAL 24 HOUR OR ((title LIKE '%%Leave%%' OR title LIKE '%%leave%%') AND DATE(created_at) < CURDATE()))", (session["household_id"],))
            mysql.connection.commit()
        except:
            mysql.connection.rollback()

        cursor.execute("""
            SELECT title, message, created_at 
            FROM notifications 
            WHERE household_id=%s AND created_at >= NOW() - INTERVAL 24 HOUR AND NOT ((title LIKE '%%Leave%%' OR title LIKE '%%leave%%') AND DATE(created_at) < CURDATE())
            ORDER BY created_at DESC LIMIT 10
        """, (session["household_id"],))
        notifications = cursor.fetchall()

        cursor.execute("""
            SELECT COUNT(*) as cnt 
            FROM notifications 
            WHERE household_id=%s AND created_at >= NOW() - INTERVAL 24 HOUR AND NOT ((title LIKE '%%Leave%%' OR title LIKE '%%leave%%') AND DATE(created_at) < CURDATE())
        """, (session["household_id"],))
        today_notif = cursor.fetchone()['cnt']

        festive_mode = 0
        try:
            cursor.execute("SELECT festive_mode FROM system_config WHERE id=1")
            fm_row = cursor.fetchone()
            festive_mode = fm_row['festive_mode'] if fm_row else 0
        except: festive_mode = 0

        cursor.close()
        return render_template("household_dashboard.html", household=household, pickup_status=pickup_status, active_pickup=active_pickup, assigned_worker=assigned_worker, points=points, categories=categories, completed_count=completed_count, total_weight=total_weight, co2_saved=co2_saved, trees_eq=trees_eq, progress_pct=progress_pct, notifications=notifications, today_notif=today_notif, festive_mode=festive_mode)

    @app.route("/household_profile")
    def household_profile():
        if "household_id" not in session: return redirect(url_for("login"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT household_id, full_name, email, phone, upi_id, area, ward_no, house_no, aadhaar_photo, latitude, longitude, reward_wallet FROM household_users WHERE household_id=%s", (session["household_id"],))
        household = cursor.fetchone()
        cursor.close()
        return render_template("household_profile.html", household=household)

    @app.route('/household/feedback')
    def household_feedback():
        if 'household_id' not in session:
            return redirect('/household_login')
        
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        
        cur.execute("SELECT household_id, full_name, email, phone, area, ward_no, house_no, reward_wallet FROM household_users WHERE household_id = %s", (session['household_id'],))
        household = cur.fetchone()
        
        # my feedbacks
        cur.execute("SELECT * FROM feedbacks WHERE household_id=%s ORDER BY created_at DESC", (session['household_id'],))
        my_feedbacks = cur.fetchall()
        
        return render_template('household_feedback.html', household=household, my_feedbacks=my_feedbacks)

    @app.route('/household/send_feedback', methods=['POST'])
    def household_send_feedback():
        if 'household_id' not in session:
            return jsonify({"success": False, "message": "Not logged in"})
        
        data = request.get_json()
        f_type = data.get('type')
        rating = data.get('rating') if f_type == 'Rating' else None
        subject = data.get('subject') or None
        message = data.get('message') or None

        # Validation based on type
        if f_type == 'Rating':
            subject = f"{rating} Star Rating"  # auto subject
            if not rating:
                return jsonify({"success": False, "message": "Please select rating"})
        else:
            # Complaint / Suggestion needs subject
            if not subject:
                return jsonify({"success": False, "message": "Subject required"})
            if not message:
                return jsonify({"success": False, "message": "Message required"})

        cur = mysql.connection.cursor()
        cur.execute("""
            INSERT INTO feedbacks (household_id, type, rating, subject, message, status)
            VALUES (%s, %s, %s, %s, %s, 'Pending')
        """, (session['household_id'], f_type, rating, subject, message))
        mysql.connection.commit()
        msg_text = f"{f_type} sent successfully!" if f_type != 'Rating' else "Rating submitted successfully!"
        return jsonify({"success": True, "message": msg_text})




    @app.route("/rewards")
    def rewards():
        if "household_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor()
        try: cur.execute("SELECT household_id, full_name, ward_no, area, house_no, upi_id, reward_wallet FROM household_users WHERE household_id=%s", (session["household_id"],))
        except: cur.execute("SELECT household_id, full_name, ward_no, area, house_no, reward_wallet FROM household_users WHERE household_id=%s", (session["household_id"],))
        household = cur.fetchone()
        cur.execute("SELECT c.created_at, wc.category_name, c.weight_kg, c.points, w.full_name FROM reward_logs c LEFT JOIN waste_categories wc ON wc.category_id=c.category_id LEFT JOIN workers w ON w.worker_id=c.worker_id WHERE c.household_id=%s ORDER BY c.created_at DESC", (session["household_id"],))
        logs = cur.fetchall()
        cur.execute("SELECT SUM(weight_kg), SUM(points) FROM reward_logs WHERE household_id=%s", (session["household_id"],))
        totals = cur.fetchone()
        if not totals or totals[0] is None: totals = (0,0)
        try:
            cur.execute("SELECT id, points_redeemed, rupees, status, upi_id, created_at, txn_id, payment_screenshot FROM redeem_requests WHERE household_id=%s ORDER BY created_at DESC", (session["household_id"],))
            redeems = cur.fetchall()
        except: redeems = []
        cur.close()
        return render_template("rewards.html", household=household, logs=logs, redeems=redeems, totals=totals)

    @app.route("/redeem", methods=["POST"])
    def redeem():
        if "household_id" not in session: return redirect(url_for("home"))
        try:
            points = int(request.form.get("points",0))
            upi = request.form.get("upi_id","").strip()
            if not upi or "@" not in upi:
                flash("Enter valid UPI ID", "error")
                return redirect(url_for("rewards"))
            if points < 1:
                flash("Please enter at least 1 point to redeem.", "error")
                return redirect(url_for("rewards"))
            cur = mysql.connection.cursor()
            cur.execute("SELECT reward_wallet FROM household_users WHERE household_id=%s", (session["household_id"],))
            wallet = cur.fetchone()[0]
            if points > wallet:
                flash("Not enough points", "error")
                return redirect(url_for("rewards"))
            rupees = points * 1
            cur.execute("INSERT INTO redeem_requests (household_id, points_redeemed, rupees, upi_id, status) VALUES (%s,%s,%s,%s,'Pending')", (session["household_id"], points, rupees, upi))
            cur.execute("UPDATE household_users SET reward_wallet = reward_wallet - %s, upi_id=%s WHERE household_id=%s", (points, upi, session["household_id"]))
            mysql.connection.commit()
            cur.close()
            flash(f"Redeem request for ₹{rupees} sent!", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("rewards"))

    @app.route("/request_pickup", methods=["POST"])
    def request_pickup():
        if "household_id" not in session:
            return jsonify({"success": False, "message": "Session expired! Please login again"}), 401
        
        if datetime.now().weekday() == 6: # 6 = Sunday
            return jsonify({"success": False, "message": "🚫 Today is Sunday (Holiday). Pickup requests are not available on Sundays."})
        
        current_hour = datetime.now().hour
        if current_hour < 8 or current_hour >= 16:
            return jsonify({"success": False, "message": "🚫 Household pickup requests are only allowed between 8:00 AM and 4:00 PM."})
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            hid = session["household_id"]
            cursor.execute("SELECT ward_no, area FROM household_users WHERE household_id=%s", (hid,))
            row = cursor.fetchone()
            if not row:
                return jsonify({"success": False, "message": "Household not found"})
            ward_no = row['ward_no']
            area = row['area']
            try:
                cursor.execute("SELECT festive_mode FROM system_config WHERE id=1")
                f = cursor.fetchone()
                festive_mode = f['festive_mode'] if f and 'festive_mode' in f else 0
            except:
                festive_mode = 0
            limit = 35 if festive_mode == 1 else 5
            cursor.execute("SELECT id FROM pickup_requests WHERE household_id=%s AND status IN ('Pending','Assigned') AND DATE(created_at)=CURDATE() LIMIT 1", (hid,))
            if cursor.fetchone():
                return jsonify({"success": False, "message": "You already have active request for today"})
            cursor.execute("""
                SELECT w.worker_id, 
                    (SELECT COUNT(*) FROM pickup_requests pr 
                        WHERE pr.worker_id=w.worker_id AND pr.status IN ('Assigned','Completed') AND DATE(pr.created_at)=CURDATE()) as assigned_cnt
                FROM workers w 
                WHERE w.ward_no=%s AND REPLACE(LOWER(TRIM(w.area)),' ','')=REPLACE(LOWER(TRIM(%s)),' ','') 
                HAVING assigned_cnt < %s 
                ORDER BY assigned_cnt ASC, w.worker_id ASC LIMIT 1
            """, (ward_no, area, limit))
            worker = cursor.fetchone()
            worker_id = worker['worker_id'] if worker else None
            if not worker_id:
                cursor.execute("""
                    SELECT w.worker_id, 
                        (SELECT COUNT(*) FROM pickup_requests pr 
                            WHERE pr.worker_id=w.worker_id AND pr.status IN ('Assigned','Completed') AND DATE(pr.created_at)=CURDATE()) as assigned_cnt
                    FROM workers w 
                    WHERE w.ward_no=%s 
                    HAVING assigned_cnt < %s 
                    ORDER BY assigned_cnt ASC, w.worker_id ASC LIMIT 1
                """, (ward_no, limit))
                worker = cursor.fetchone()
                if worker:
                    worker_id = worker['worker_id']
            if not worker_id:
                return jsonify({"success": False, "limit_reached": True, "message": "Oops sorry, worker limit reached, please comeback tomorrow"})
            
            cursor.execute("INSERT INTO pickup_requests (household_id, worker_id, status) VALUES (%s,%s,'Pending')", (hid, worker_id))
            mysql.connection.commit()
            return jsonify({"success": True, "message": "Pickup requested!"})
        except Exception as e:
            mysql.connection.rollback()
            return jsonify({"success": False, "message": str(e)})
        finally:
            cursor.close()

    @app.route("/cancel_pickup", methods=["POST"])
    def cancel_pickup():
        if "household_id" not in session: return jsonify({"success": False, "message": "Login expired"}), 401
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("UPDATE pickup_requests SET status='Cancelled' WHERE household_id=%s AND status IN ('Pending','Assigned')", (session["household_id"],))
        mysql.connection.commit()
        cursor.close()
        return jsonify({"success": True, "message": "Pickup cancelled"})

    @app.route("/api/household/update-profile", methods=["POST"])
    def update_household_profile():
        if "household_id" not in session: return jsonify({"success": False, "message":"Login first"})
        data = request.get_json()
        name = data.get("name","").strip()
        phone = data.get("phone","").strip()
        email = data.get("email","").strip()
        upi_id = data.get("upi_id","").strip() or None
        if len(name) < 2: return jsonify({"success": False, "message":"Name min 2 chars"})
        if not re.match(r'^[0-9]{10}$', phone): return jsonify({"success": False, "message":"Invalid phone"})
        if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email): return jsonify({"success": False, "message":"Invalid email"})
        if upi_id and "@" not in upi_id: return jsonify({"success": False, "message":"Invalid UPI ID"})
        cur = mysql.connection.cursor()
        cur.execute("SELECT household_id FROM household_users WHERE email=%s AND household_id!=%s", (email, session["household_id"]))
        if cur.fetchone():
            cur.close()
            return jsonify({"success": False, "message":"Email already used"})
        cur.execute("UPDATE household_users SET full_name=%s, phone=%s, email=%s, upi_id=%s WHERE household_id=%s", (name, phone, email, upi_id, session["household_id"]))
        mysql.connection.commit()
        cur.close()
        session["household_name"] = name
        return jsonify({"success": True, "message":"Profile updated"})

    # ── Change Password: Household ─────────────────────────────────────────
    @app.route("/api/household/change-password", methods=["POST"])
    def household_change_password():
        if "household_id" not in session:
            return jsonify({"success": False, "message": "Login first"})
        data = request.get_json() or {}
        old_pw  = data.get("old_password", "")
        new_pw  = data.get("new_password", "").strip()
        conf_pw = data.get("confirm_password", "").strip()
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT password FROM household_users WHERE household_id=%s", (session["household_id"],))
        row = cur.fetchone()
        if not row or row["password"] != old_pw:
            cur.close()
            return jsonify({"success": False, "message": "Current password is incorrect."})
        if len(new_pw) < 6:
            cur.close()
            return jsonify({"success": False, "message": "New password must be at least 6 characters."})
        if new_pw != conf_pw:
            cur.close()
            return jsonify({"success": False, "message": "New passwords do not match."})
        if new_pw == old_pw:
            cur.close()
            return jsonify({"success": False, "message": "New password must be different from the current password."})
        cur.execute("UPDATE household_users SET password=%s WHERE household_id=%s", (new_pw, session["household_id"]))
        mysql.connection.commit()
        cur.close()
        return jsonify({"success": True, "message": "Password changed successfully!"})

    # ── Change Password: Worker ────────────────────────────────────────────
    @app.route("/api/worker/change-password", methods=["POST"])
    def worker_change_password():
        if "worker_id" not in session:
            return jsonify({"success": False, "message": "Login first"})
        data = request.get_json() or {}
        old_pw  = data.get("old_password", "")
        new_pw  = data.get("new_password", "").strip()
        conf_pw = data.get("confirm_password", "").strip()
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT password FROM workers WHERE worker_id=%s", (session["worker_id"],))
        row = cur.fetchone()
        if not row or row["password"] != old_pw:
            cur.close()
            return jsonify({"success": False, "message": "Current password is incorrect."})
        if len(new_pw) < 6:
            cur.close()
            return jsonify({"success": False, "message": "New password must be at least 6 characters."})
        if new_pw != conf_pw:
            cur.close()
            return jsonify({"success": False, "message": "New passwords do not match."})
        if new_pw == old_pw:
            cur.close()
            return jsonify({"success": False, "message": "New password must be different from the current password."})
        cur.execute("UPDATE workers SET password=%s WHERE worker_id=%s", (new_pw, session["worker_id"]))
        mysql.connection.commit()
        cur.close()
        return jsonify({"success": True, "message": "Password changed successfully!"})

    # ── Change Password: Staff ─────────────────────────────────────────────
    @app.route("/api/staff/change-password", methods=["POST"])
    def staff_change_password():
        if "staff_id" not in session:
            return jsonify({"success": False, "message": "Login first"})
        data = request.get_json() or {}
        old_pw  = data.get("old_password", "")
        new_pw  = data.get("new_password", "").strip()
        conf_pw = data.get("confirm_password", "").strip()
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT password FROM staff WHERE staff_id=%s", (session["staff_id"],))
        row = cur.fetchone()
        if not row or row["password"] != old_pw:
            cur.close()
            return jsonify({"success": False, "message": "Current password is incorrect."})
        if len(new_pw) < 6:
            cur.close()
            return jsonify({"success": False, "message": "New password must be at least 6 characters."})
        if new_pw != conf_pw:
            cur.close()
            return jsonify({"success": False, "message": "New passwords do not match."})
        if new_pw == old_pw:
            cur.close()
            return jsonify({"success": False, "message": "New password must be different from the current password."})
        cur.execute("UPDATE staff SET password=%s WHERE staff_id=%s", (new_pw, session["staff_id"]))
        mysql.connection.commit()
        cur.close()
        return jsonify({"success": True, "message": "Password changed successfully!"})

    # ── Change Password: Admin ─────────────────────────────────────────────
    @app.route("/api/admin/change-password", methods=["POST"])
    def admin_change_password():
        if "admin_id" not in session:
            return jsonify({"success": False, "message": "Login first"})
        data = request.get_json() or {}
        old_pw  = data.get("old_password", "")
        new_pw  = data.get("new_password", "").strip()
        conf_pw = data.get("confirm_password", "").strip()
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT password FROM admin WHERE admin_id=%s", (session["admin_id"],))
        row = cur.fetchone()
        if not row or row["password"] != old_pw:
            cur.close()
            return jsonify({"success": False, "message": "Current password is incorrect."})
        if len(new_pw) < 6:
            cur.close()
            return jsonify({"success": False, "message": "New password must be at least 6 characters."})
        if new_pw != conf_pw:
            cur.close()
            return jsonify({"success": False, "message": "New passwords do not match."})
        if new_pw == old_pw:
            cur.close()
            return jsonify({"success": False, "message": "New password must be different from the current password."})
        cur.execute("UPDATE admin SET password=%s WHERE admin_id=%s", (new_pw, session["admin_id"]))
        mysql.connection.commit()
        cur.close()
        return jsonify({"success": True, "message": "Password changed successfully!"})

    @app.route("/worker_dashboard")
    def worker_dashboard():
        if "worker_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            auto_reassign_expired_requests(cursor)
            mysql.connection.commit()
        except: mysql.connection.rollback()
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT * FROM workers WHERE worker_id=%s", (session["worker_id"],))
        worker = cursor.fetchone()
        if not worker:
            session.clear()
            return redirect(url_for("home"))
        area = worker['area']
        ward_no = worker['ward_no']
        cursor.execute("SELECT household_id, full_name, area, ward_no, house_no, phone FROM household_users WHERE ward_no=%s AND REPLACE(LOWER(TRIM(area)),' ','')=REPLACE(LOWER(TRIM(%s)),' ','') AND registration_status='Approved'", (ward_no, area))
        households = cursor.fetchall()
        try:
            cursor.execute("SELECT pr.id, hu.full_name, hu.house_no, hu.phone, hu.area, hu.ward_no, hu.household_id, pr.status, hu.latitude, hu.longitude, pr.created_at FROM pickup_requests pr JOIN household_users hu ON pr.household_id=hu.household_id WHERE pr.worker_id=%s AND pr.status='Assigned' AND (DATE(pr.created_at)=CURDATE() OR DATE(pr.updated_at)=CURDATE()) ORDER BY pr.created_at ASC", (session["worker_id"],))
            assigned_pickups = cursor.fetchall()
        except: assigned_pickups = []
        try:
            cursor.execute("SELECT pr.id, hu.full_name, hu.house_no, hu.phone, hu.area, hu.ward_no, hu.household_id, pr.status, hu.latitude, hu.longitude, pr.created_at, pr.updated_at, pr.is_reassigned, pr.reassign_count FROM pickup_requests pr JOIN household_users hu ON pr.household_id=hu.household_id WHERE pr.status='Pending' AND (pr.worker_id=%s OR (pr.worker_id IS NULL AND hu.ward_no=%s AND REPLACE(LOWER(TRIM(hu.area)),' ','')=REPLACE(LOWER(TRIM(%s)),' ',''))) ORDER BY pr.created_at ASC", (session["worker_id"], ward_no, area))
            incoming_requests = cursor.fetchall()
        except: incoming_requests = []
        cursor.execute("SELECT category_id, category_name, points_per_kg FROM waste_categories ORDER BY category_name")
        reward_cats = cursor.fetchall()
        try:
            cursor.execute("SELECT festive_mode FROM system_config WHERE id=1")
            festive_row = cursor.fetchone()
            festive_mode = festive_row['festive_mode'] if festive_row else 0
        except: festive_mode = 0

        max_limit = 35 if festive_mode == 1 else 5
        cursor.execute("SELECT COUNT(*) as c FROM pickup_requests WHERE worker_id=%s AND status='Completed' AND completed_date=CURDATE()", (session["worker_id"],))
        comp_row = cursor.fetchone()
        completed_today = comp_row['c'] if comp_row else 0
        remaining_limit = max(0, max_limit - completed_today)

        cursor.close()
        return render_template("worker_dashboard.html",
                               worker=worker, households=households,
                               assigned_pickups=assigned_pickups,
                               incoming_requests=incoming_requests,
                               reward_cats=reward_cats,
                               festive_mode=festive_mode,
                               completed_today=completed_today,
                               max_limit=max_limit,
                               remaining_limit=remaining_limit,
                               today_date=datetime.now().strftime("%d %B %Y"))

    @app.route("/worker/accept_pickup/<int:pid>", methods=["POST"])
    def accept_pickup(pid):
        if "worker_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT festive_mode FROM system_config WHERE id=1")
        f = cursor.fetchone()
        festive_mode = f['festive_mode'] if f else 0
        limit = 35 if festive_mode==1 else 5
        cursor.execute("SELECT COUNT(*) as c FROM pickup_requests WHERE worker_id=%s AND status IN ('Assigned','Completed') AND DATE(created_at)=CURDATE()", (session["worker_id"],))
        count = cursor.fetchone()['c']
        if count >= limit:
            flash(f"Daily limit reached ({limit} houses)! Cannot accept more pickups today.", "worker")
            try:
                cursor.execute("""
                    SELECT pr.household_id FROM pickup_requests pr 
                    JOIN household_users hu ON hu.household_id=pr.household_id 
                    WHERE pr.status='Pending' AND (pr.worker_id=%s OR pr.worker_id IS NULL) AND DATE(pr.created_at)=CURDATE()
                """, (session["worker_id"],))
                pending_houses = cursor.fetchall()
                for ph in pending_houses:
                    cursor.execute("""
                        INSERT INTO notifications (worker_id, household_id, title, message)
                        VALUES (%s, %s, %s, %s)
                    """, (session["worker_id"], ph['household_id'], "Worker Daily Limit Reached", "Oops sorry, worker limit reached, please comeback tomorrow"))
                mysql.connection.commit()
            except Exception as e:
                print("notify pending households error", e)
            cursor.close()
            return redirect(url_for("worker_dashboard"))
        cursor.execute("UPDATE pickup_requests SET status='Assigned', worker_id=%s, updated_at=NOW() WHERE id=%s AND status='Pending'", (session["worker_id"], pid))
        mysql.connection.commit()
        cursor.close()
        return redirect(url_for("worker_dashboard"))

    @app.route("/worker/update_pickup/<int:pid>/<status>", methods=["POST"])
    def update_pickup(pid, status):
        if "worker_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor()
        if status in ['Not Home', 'Not at Home']:
            status = 'Not at Home'
        else:
            status = 'Completed'
        if status == 'Completed': cur.execute("UPDATE pickup_requests SET status=%s, completed_date=CURDATE(), updated_at=NOW() WHERE id=%s", (status, pid))
        else: cur.execute("UPDATE pickup_requests SET status=%s, completed_date=NULL, updated_at=NOW() WHERE id=%s", (status, pid))
        mysql.connection.commit()
        cur.close()
        return redirect(url_for("worker_dashboard"))

    @app.route("/worker/update_location", methods=["POST"])
    def update_worker_location():
        if "worker_id" not in session: return jsonify({"success":False}), 401
        data = request.get_json()
        cur = mysql.connection.cursor()
        cur.execute("UPDATE workers SET latitude=%s, longitude=%s WHERE worker_id=%s", (data['lat'], data['lng'], session['worker_id']))
        mysql.connection.commit()
        cur.close()
        return "ok"

    @app.route("/worker/rewards/<int:pickup_id>", methods=["GET","POST"])
    def worker_rewards(pickup_id):
        if "worker_id" not in session: 
            return redirect(url_for("home"))
        
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT pr.id, hu.household_id, hu.full_name, hu.house_no, hu.area, hu.reward_wallet FROM pickup_requests pr JOIN household_users hu ON hu.household_id=pr.household_id WHERE pr.id=%s", (pickup_id,))
        data = cur.fetchone()
        if not data:
            cur.close()
            flash("Pickup not found", "error")
            return redirect(url_for("worker_dashboard"))

        cur.execute("SELECT category_id, category_name, points_per_kg, is_disposal FROM waste_categories ORDER BY category_name ASC")
        categories = cur.fetchall()

        if request.method == "POST":
            try:
                cat_ids = request.form.getlist("category_id[]")
                weights = request.form.getlist("weight[]")
                
                total_pts = 0
                total_w = 0
                total_disposal = 0

                for cid, w in zip(cat_ids, weights):
                    if not cid or not w:
                        continue
                    weight = float(w)
                    if weight <= 0:
                        continue

                    cur.execute("SELECT points_per_kg, is_disposal FROM waste_categories WHERE category_id=%s", (cid,))
                    cat = cur.fetchone()
                    if not cat:
                        continue

                    # calculate points
                    if int(cat['is_disposal'] or 0) == 1:
                        pts = 0
                        total_disposal += weight
                    else:
                        pts = int(weight * float(cat['points_per_kg'] or 0))
                        total_pts += pts
                        total_w += weight

                    # YOUR TABLE HAS NO points_per_kg COLUMN - so don't insert it
                    cur.execute("""
                        INSERT INTO reward_logs (household_id, worker_id, pickup_id, category_id, weight_kg, points)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """, (data['household_id'], session["worker_id"], pickup_id, cid, weight, pts))

                # update wallet only for recyclable points
                if total_pts > 0:
                    cur.execute("UPDATE household_users SET reward_wallet = reward_wallet + %s WHERE household_id=%s", 
                                (total_pts, data['household_id']))

                cur.execute("UPDATE pickup_requests SET status='Completed', completed_date=CURDATE(), updated_at=NOW() WHERE id=%s", (pickup_id,))
                mysql.connection.commit()
                
                flash(f"Success! {total_w}kg = {total_pts} pts added + {total_disposal}kg disposal", "worker")
                cur.close()
                return redirect(url_for("worker_dashboard"))

            except Exception as e:
                print("worker_rewards error:", e)
                import traceback; traceback.print_exc()
                mysql.connection.rollback()
                flash(f"DB Error: {e}", "error")

        cur.close()
        return render_template("add_reward.html", pickup=data, categories=categories)
        




    @app.route('/worker/history')
    @app.route('/worker_viewhistory')
    def worker_history():
        if "worker_id" not in session:
            return redirect(url_for("home"))
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        worker_id = session["worker_id"]
        
        # Get worker - same as dashboard
        cursor.execute("SELECT * FROM workers WHERE worker_id=%s", (worker_id,))
        worker = cursor.fetchone()
        if not worker:
            cursor.close()
            session.clear()
            return redirect(url_for("home"))

        filter_type = request.args.get('filter', 'all')
        today = datetime.now().date()

        # History query - from your actual tables
        sql = """
            SELECT pr.id, pr.status, pr.created_at as date, pr.completed_date,
                   hu.full_name, hu.house_no, hu.ward_no, hu.area,
                   COALESCE(SUM(rl.points),0) as points,
                   GROUP_CONCAT(wc.category_name SEPARATOR ', ') as waste_type
            FROM pickup_requests pr
            JOIN household_users hu ON hu.household_id = pr.household_id
            LEFT JOIN reward_logs rl ON rl.pickup_id = pr.id
            LEFT JOIN waste_categories wc ON wc.category_id = rl.category_id
            WHERE pr.worker_id = %s
        """
        params = [worker_id]

        if filter_type == 'today':
            sql += " AND DATE(pr.completed_date) = %s OR DATE(pr.created_at) = %s"
            params.extend([today, today])
        elif filter_type == 'week':
            last_7 = today - timedelta(days=7)
            sql += " AND (DATE(pr.completed_date) >= %s OR DATE(pr.created_at) >= %s)"
            params.extend([last_7, last_7])
        elif filter_type == 'completed':
            sql += " AND pr.status = 'Completed'"

        sql += " GROUP BY pr.id ORDER BY pr.created_at DESC"

        cursor.execute(sql, params)
        history = cursor.fetchall()
        cursor.close()

        total_count = len([h for h in history if h['status'] == 'Completed'])
        total_points = sum([h.get('points', 0) or 0 for h in history])

        return render_template("worker_viewhistory.html", 
                               history=history, 
                               total_count=total_count, 
                               total_points=total_points,
                               worker=worker,
                               filter_type=filter_type,
                               today_date=today.strftime('%d %b %Y'))



    @app.route("/worker_viewhouseholds")
    def worker_viewhouseholds():
        if "worker_id" not in session: return redirect(url_for("home"))
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT * FROM workers WHERE worker_id=%s", (session["worker_id"],))
        worker = cursor.fetchone()
        ward_no = worker['ward_no']; area = worker['area']
        cursor.execute("SELECT household_id, full_name, house_no, phone, area, email, ward_no, reward_wallet, latitude, longitude FROM household_users WHERE ward_no=%s AND area=%s AND registration_status='Approved' ORDER BY house_no ASC", (ward_no, area))
        all_households = cursor.fetchall()
        cursor.close()
        return render_template("worker_viewhouseholds.html", worker=worker, all_households=all_households, today_date=datetime.now().strftime("%d %B %Y"))

    @app.route("/worker_profile")
    def worker_profile():
        if "worker_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM workers WHERE worker_id=%s", (session["worker_id"],))
        worker = cur.fetchone()
        cur.close()
        return render_template("worker_profile.html", worker=worker)

    @app.route("/admin/mark_leave/<int:worker_id>", methods=["POST"])
    def admin_mark_leave(worker_id):
        if "admin_id" not in session:
            return redirect(url_for("login"))
        reason = request.form.get("reason","").strip() or "On Leave"
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT ward_no, area FROM workers WHERE worker_id=%s", (worker_id,))
        w = cur.fetchone()
        if not w:
            cur.close()
            return redirect(url_for("view_workers"))
        cur.execute("INSERT INTO worker_leaves (worker_id, ward_no, area, leave_date, reason) VALUES (%s,%s,%s,CURDATE(),%s)",
                    (worker_id, w['ward_no'], w['area'], reason))
        cur.execute("SELECT household_id FROM household_users WHERE ward_no=%s AND area=%s AND registration_status='Approved'", (w['ward_no'], w['area']))
        households = cur.fetchall()
        title = "Worker On Leave Today"
        message = f"Your pickup worker is on leave today ({datetime.now().strftime('%d %b %Y')}). Alternate worker will collect your waste. Thank you."
        for h in households:
            cur.execute("INSERT INTO notifications (worker_id, household_id, title, message) VALUES (%s,%s,%s,%s)",
                        (worker_id, h['household_id'], title, message))
        cur.execute("""
            UPDATE pickup_requests pr
            JOIN (
                SELECT pr2.id FROM pickup_requests pr2
                JOIN household_users hu ON hu.household_id=pr2.household_id
                WHERE pr2.worker_id=%s AND pr2.scheduled_date=CURDATE()
                AND pr2.status IN ('Pending','Assigned')
                AND hu.ward_no=%s AND hu.area=%s
            ) t ON t.id=pr.id
            SET pr.reassign_count=pr.reassign_count+1
        """, (worker_id, w['ward_no'], w['area']))
        mysql.connection.commit()
        cur.close()
        flash(f"Worker {worker_id} marked on leave. {len(households)} households notified without reason.", "success")
        return redirect(url_for("view_workers"))

    @app.route("/worker/notify_leave", methods=["GET","POST"])
    def worker_notify_leave():
        if "worker_id" not in session:
            return redirect(url_for("login"))
        if request.method == "POST":
            leave_date = request.form.get("leave_date", "").strip() or datetime.now().strftime("%Y-%m-%d")
            reason = request.form.get("reason", "").strip() or "Personal Leave"
            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            try:
                cur.execute("ALTER TABLE worker_leaves ADD COLUMN status VARCHAR(20) DEFAULT 'Approved'")
            except: pass
            try:
                cur.execute("ALTER TABLE worker_leaves ADD COLUMN notified TINYINT(1) DEFAULT 0")
            except: pass

            cur.execute("SELECT ward_no, area FROM workers WHERE worker_id=%s", (session["worker_id"],))
            w = cur.fetchone()
            if w:
                cur.execute("SELECT id FROM worker_leaves WHERE worker_id=%s AND leave_date=%s", (session["worker_id"], leave_date))
                if cur.fetchone():
                    flash("You have already submitted a leave request for this date.", "error")
                else:
                    cur.execute("INSERT INTO worker_leaves (worker_id, ward_no, area, leave_date, reason, status, notified) VALUES (%s,%s,%s,%s,%s,'Pending',0)",
                                (session["worker_id"], w['ward_no'], w['area'], leave_date, reason))
                    mysql.connection.commit()
                    flash("Leave request submitted successfully to Admin for approval!", "success")
            cur.close()
            return redirect(url_for("worker_leave_dates"))
        return redirect(url_for("worker_leave_dates"))

    @app.route("/worker/leave_dates")
    def worker_leave_dates():
        if "worker_id" not in session:
            return redirect(url_for("login"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            cur.execute("ALTER TABLE worker_leaves ADD COLUMN status VARCHAR(20) DEFAULT 'Approved'")
        except: pass
        try:
            cur.execute("ALTER TABLE worker_leaves ADD COLUMN notified TINYINT(1) DEFAULT 0")
        except: pass

        cur.execute("SELECT worker_id, full_name, ward_no, area FROM workers WHERE worker_id=%s", (session["worker_id"],))
        worker = cur.fetchone()
        cur.execute("""
            SELECT wl.*, 
                IFNULL(wl.status, 'Accepted') as status,
                DATE_FORMAT(wl.created_at, '%%h:%%i %%p') as time,
                (SELECT COUNT(*) FROM notifications 
                    WHERE worker_id=wl.worker_id AND DATE(created_at)=wl.leave_date) as notified_count
            FROM worker_leaves wl 
            WHERE wl.worker_id=%s 
            ORDER BY wl.leave_date DESC, wl.created_at DESC
        """, (session["worker_id"],))
        leaves = cur.fetchall()
        cur.close()
        return render_template("worker_leave_dates.html", 
                            worker=worker, 
                            leaves=leaves, 
                            today_date=datetime.now().strftime("%d %b %Y"),
                            raw_today_date=datetime.now().strftime("%Y-%m-%d"))

    @app.route("/admin/worker_leaves", methods=["GET", "POST"])
    def admin_worker_leaves():
        if "admin_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            cur.execute("ALTER TABLE worker_leaves ADD COLUMN status VARCHAR(20) DEFAULT 'Approved'")
        except: pass
        try:
            cur.execute("ALTER TABLE worker_leaves ADD COLUMN notified TINYINT(1) DEFAULT 0")
        except: pass

        if request.method == "POST":
            worker_id = request.form.get("worker_id")
            leave_date = request.form.get("leave_date") or datetime.now().strftime("%Y-%m-%d")
            reason = request.form.get("reason", "").strip() or "On Leave"

            if worker_id:
                cur.execute("SELECT ward_no, area FROM workers WHERE worker_id=%s", (worker_id,))
                w = cur.fetchone()
                if w:
                    cur.execute("SELECT id FROM worker_leaves WHERE worker_id=%s AND leave_date=%s", (worker_id, leave_date))
                    if not cur.fetchone():
                        cur.execute("INSERT INTO worker_leaves (worker_id, ward_no, area, leave_date, reason, status, notified) VALUES (%s,%s,%s,%s,%s,'Accepted',0)",
                                    (worker_id, w['ward_no'], w['area'], leave_date, reason))
                        mysql.connection.commit()
                        flash(f"Worker leave recorded as Accepted.", "success")
                    else:
                        flash("Worker is already marked on leave for this date.", "error")

        # Workers list for dropdown selector
        cur.execute("SELECT worker_id, full_name, ward_no, area, phone FROM workers ORDER BY ward_no ASC, full_name ASC")
        workers_list = cur.fetchall()

        # All leave records
        cur.execute("""
            SELECT wl.id, wl.leave_date, wl.worker_id, w.full_name, w.ward_no, w.area, w.phone, wl.reason, wl.created_at,
                   IFNULL(wl.status, 'Accepted') as status, IFNULL(wl.notified, 0) as notified,
                   (SELECT COUNT(*) FROM notifications n WHERE n.worker_id=wl.worker_id AND DATE(n.created_at)=wl.leave_date) as notified_count
            FROM worker_leaves wl
            JOIN workers w ON w.worker_id = wl.worker_id
            ORDER BY wl.leave_date DESC, wl.created_at DESC
        """)
        all_leaves = cur.fetchall()

        cur.execute("SELECT COUNT(*) as cnt FROM worker_leaves WHERE leave_date = CURDATE() AND IFNULL(status,'Approved') IN ('Accepted','Approved')")
        today_leaves_count = cur.fetchone()['cnt']

        cur.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        household_pending = cur.fetchone()['cnt']

        cur.close()
        return render_template("admin_worker_leaves.html",
                               leaves=all_leaves,
                               workers_list=workers_list,
                               today_leaves_count=today_leaves_count,
                               total_leaves_count=len(all_leaves),
                               total_workers=len(workers_list),
                               pending_count=household_pending,
                               today_date=datetime.now().strftime("%Y-%m-%d"))

    @app.route("/admin/accept_leave/<int:leave_id>")
    def admin_accept_leave(leave_id):
        if "admin_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor()
        cur.execute("UPDATE worker_leaves SET status='Accepted' WHERE id=%s", (leave_id,))
        mysql.connection.commit()
        cur.close()
        flash("Worker leave request accepted!", "success")
        return redirect(url_for("admin_worker_leaves"))

    @app.route("/admin/notify_leave/<int:leave_id>")
    def admin_notify_leave(leave_id):
        if "admin_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT wl.*, w.full_name FROM worker_leaves wl JOIN workers w ON w.worker_id=wl.worker_id WHERE wl.id=%s", (leave_id,))
        leave = cur.fetchone()
        if leave:
            worker_id = leave['worker_id']
            ward_no = leave['ward_no']
            area = leave['area']
            leave_date = leave['leave_date']

            cur.execute("SELECT household_id FROM household_users WHERE ward_no=%s AND REPLACE(LOWER(TRIM(area)),' ','')=REPLACE(LOWER(TRIM(%s)),' ','') AND registration_status='Approved'", (ward_no, area))
            households = cur.fetchall()
            title = "Worker On Leave Today"
            message = f"Your pickup worker is on leave today ({leave_date}). Alternate worker will collect your waste. Thank you."
            for h in households:
                cur.execute("INSERT INTO notifications (worker_id, household_id, title, message) VALUES (%s,%s,%s,%s)",
                            (worker_id, h['household_id'], title, message))

            auto_reassign_expired_requests(cur)
            cur.execute("UPDATE worker_leaves SET notified=1, status='Accepted' WHERE id=%s", (leave_id,))
            mysql.connection.commit()
            flash(f"{len(households)} households notified and pending pickups auto-reassigned for {leave['full_name']}!", "success")
        cur.close()
        return redirect(url_for("admin_worker_leaves"))

    @app.route("/admin/delete_leave/<int:leave_id>")
    def admin_delete_leave(leave_id):
        if "admin_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor()
        cur.execute("DELETE FROM worker_leaves WHERE id=%s", (leave_id,))
        mysql.connection.commit()
        cur.close()
        flash("Worker leave entry removed.", "success")
        return redirect(url_for("admin_worker_leaves"))

    
    @app.route("/admin/collhistory")
    @app.route("/admin_collhistory")
    @app.route("/admin/totalwastes")
    def admin_collhistory():
        if "admin_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("""
            SELECT COALESCE(c.pickup_id, c.id) as group_pickup_id,
                   MAX(c.created_at) as created_at, hu.full_name as household_name, hu.house_no, hu.ward_no, hu.area, hu.phone as household_phone,
                   w.full_name as worker_name, w.phone as worker_phone, GROUP_CONCAT(DISTINCT wc.category_name SEPARATOR ', ') as waste_type,
                   GROUP_CONCAT(CONCAT(c.weight_kg + 0, ' kg (', wc.category_name, ')') SEPARATOR ', ') as weight_details,
                   SUM(c.weight_kg) as weight_kg, SUM(c.points) as points
            FROM reward_logs c
            JOIN household_users hu ON hu.household_id = c.household_id
            LEFT JOIN workers w ON w.worker_id = c.worker_id
            LEFT JOIN waste_categories wc ON wc.category_id = c.category_id
            GROUP BY COALESCE(c.pickup_id, c.id), hu.household_id, hu.full_name, hu.house_no, hu.ward_no, hu.area, hu.phone, w.worker_id, w.full_name, w.phone
            ORDER BY created_at DESC
        """)
        all_collected_wastes = cur.fetchall()

        cur.execute("""
            SELECT COALESCE(SUM(weight_kg),0) as total_kg, 
                   COALESCE(SUM(points),0) as total_pts, 
                   COUNT(DISTINCT COALESCE(pickup_id, id)) as total_cnt 
            FROM reward_logs
        """)
        totals = cur.fetchone()

        cur.execute("SELECT COUNT(*) as cnt FROM household_users WHERE registration_status='Pending'")
        household_pending = cur.fetchone()['cnt']

        cur.close()
        return render_template("admin_totalwastes.html",
                               all_collected_wastes=all_collected_wastes,
                               totals=totals,
                               pending_count=household_pending,
                               today_date=datetime.now().strftime("%d %b %Y"))

    def admin_totalwastes():
        return admin_collhistory()

    @app.route("/staff/totalwastes")
    def staff_totalwastes():
        if "staff_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("""
            SELECT COALESCE(c.pickup_id, c.id) as group_pickup_id,
                   MAX(c.created_at) as created_at, hu.full_name as household_name, hu.house_no, hu.ward_no, hu.area,
                   w.full_name as worker_name, GROUP_CONCAT(DISTINCT wc.category_name SEPARATOR ', ') as category_name,
                   GROUP_CONCAT(CONCAT(c.weight_kg + 0, ' kg (', wc.category_name, ')') SEPARATOR ', ') as weight_details,
                   SUM(c.weight_kg) as weight_kg, SUM(c.points) as points
            FROM reward_logs c
            JOIN household_users hu ON hu.household_id = c.household_id
            LEFT JOIN waste_categories wc ON wc.category_id = c.category_id
            LEFT JOIN workers w ON w.worker_id = c.worker_id
            GROUP BY COALESCE(c.pickup_id, c.id), hu.household_id, hu.full_name, hu.house_no, hu.ward_no, hu.area, w.worker_id, w.full_name
            ORDER BY created_at DESC
        """)
        all_collected_wastes = cur.fetchall()

        cur.execute("""
            SELECT COALESCE(SUM(weight_kg),0) as total_kg, 
                   COUNT(DISTINCT COALESCE(pickup_id, id)) as total_cnt 
            FROM reward_logs
        """)
        totals = cur.fetchone()

        cur.close()
        return render_template("staff_totalwastes.html",
                               all_collected_wastes=all_collected_wastes,
                               totals=totals,
                               staff_name=session.get("staff_name","Staff"),
                               today_date=datetime.now().strftime("%d %b %Y"))

    @app.route("/staff/recycling_log")
    def staff_recycling_log():
        if "staff_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS raw_waste_usage_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    category_id INT,
                    recycled_id INT,
                    staff_id INT,
                    kg_used DECIMAL(10,2),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        except: pass
        cur.execute("""
            SELECT r.id, r.category_id, r.kg_used, r.created_at,
                   wc.category_name,
                   COALESCE(st.full_name, CONCAT('Staff #', r.staff_id)) as staff_name
            FROM raw_waste_usage_log r
            LEFT JOIN waste_categories wc ON wc.category_id = r.category_id
            LEFT JOIN staff st ON st.staff_id = r.staff_id
            ORDER BY r.created_at DESC
        """)
        logs = cur.fetchall()

        cur.execute("SELECT COALESCE(SUM(kg_used),0) as total_recycled_kg, COUNT(*) as total_cnt FROM raw_waste_usage_log")
        stats = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(kg_used),0) as today_recycled_kg FROM raw_waste_usage_log WHERE DATE(created_at)=CURDATE()")
        today_stats = cur.fetchone()
        cur.close()

        return render_template("staff_recycling_log.html",
                               logs=logs,
                               total_recycled_kg=stats['total_recycled_kg'] if stats else 0,
                               total_cnt=stats['total_cnt'] if stats else 0,
                               today_recycled_kg=today_stats['today_recycled_kg'] if today_stats else 0,
                               staff_name=session.get("staff_name","Staff"),
                               today_date=datetime.now().strftime("%d %b %Y"))

    @app.route("/api/staff/update-profile", methods=["POST"])
    def staff_update_profile():
        if "staff_id" not in session:
            return jsonify({"success": False, "message": "Session expired! Please login again"})
        try:
            data = request.get_json() or {}
            full_name = data.get("full_name","").strip()
            email = data.get("email","").strip()
            phone = data.get("phone","").strip()
            street = data.get("street","").strip()
            city = data.get("city","").strip()
            district = data.get("district","").strip()
            pincode = data.get("pincode","").strip()
            qualification = data.get("qualification","").strip()

            if len(full_name) < 2:
                return jsonify({"success": False, "message": "Name must be at least 2 characters"})
            if not re.match(r'^[6-9]\d{9}$', phone):
                return jsonify({"success": False, "message": "Phone must be 10 digits starting with 6-9"})
            if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
                return jsonify({"success": False, "message": "Invalid email address"})
            if not re.match(r'^\d{6}$', pincode):
                return jsonify({"success": False, "message": "Pincode must be 6 digits"})

            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # phone unique
            cur.execute("SELECT staff_id FROM staff WHERE phone=%s AND staff_id!=%s UNION SELECT worker_id FROM workers WHERE phone=%s UNION SELECT household_id FROM household_users WHERE phone=%s", (phone, session["staff_id"], phone, phone))
            if cur.fetchone():
                cur.close()
                return jsonify({"success": False, "message": f"Phone {phone} already exists!"})

            # email unique
            cur.execute("SELECT staff_id FROM staff WHERE LOWER(email)=LOWER(%s) AND staff_id!=%s UNION SELECT worker_id FROM workers WHERE LOWER(email)=LOWER(%s) UNION SELECT household_id FROM household_users WHERE LOWER(email)=LOWER(%s)", (email, session["staff_id"], email, email))
            if cur.fetchone():
                cur.close()
                return jsonify({"success": False, "message": f"Email {email} already exists!"})

            cur.execute("UPDATE staff SET full_name=%s, email=%s, phone=%s, street=%s, city=%s, district=%s, pincode=%s, qualification=%s WHERE staff_id=%s", (full_name, email, phone, street, city, district, pincode, qualification, session["staff_id"]))
            mysql.connection.commit()
            cur.close()
            session["staff_name"] = full_name
            return jsonify({"success": True, "message": "Profile updated successfully!"})

        except Exception as e:
            print("staff_update_profile ERROR:", e)
            import traceback; traceback.print_exc()
            try:
                mysql.connection.rollback()
            except:
                pass
            return jsonify({"success": False, "message": f"Error: {str(e)}"})



    @app.route("/staff/sales")
    @app.route("/staff_sales")
    def staff_sales():
        if "staff_id" not in session:
            return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        search = request.args.get('search', '').strip()
        query = """
            SELECT s.id, s.recycled_id, s.staff_id, s.type, s.qty_kg, s.qty_count,
                s.rate_per_kg, s.rate_per_count, s.amount, s.buyer_name, s.buyer_company,
                s.buyer_contact, s.buyer_place, s.payment_mode, s.vehicle_no, s.gst_percent,
                s.description, s.created_at,
                COALESCE(rp.recycled_name, s.buyer_name, 'Disposal / Raw Waste') as recycled_name,
                COALESCE(rp.unit, 'kg') as unit,
                st.full_name as staff_name
            FROM sales_log s
            LEFT JOIN recycled_products rp ON rp.recycled_id = s.recycled_id
            LEFT JOIN staff st ON st.staff_id = s.staff_id
            WHERE s.type='resale'
        """
        params = []
        if search:
            query += " AND (rp.recycled_name LIKE %s OR s.buyer_company LIKE %s OR s.buyer_name LIKE %s OR st.full_name LIKE %s OR s.payment_mode LIKE %s OR s.vehicle_no LIKE %s OR s.buyer_place LIKE %s)"
            like = f"%{search}%"
            params.extend([like, like, like, like, like, like, like])
        query += " ORDER BY s.created_at DESC LIMIT 500"
        cur.execute(query, params)
        sales = cur.fetchall()

        cur.execute("SELECT COALESCE(SUM(amount),0) as total, COALESCE(SUM(qty_kg),0) as kg, COUNT(*) as cnt FROM sales_log WHERE type='resale'")
        total_stats = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(amount),0) as today_rev, COALESCE(SUM(qty_kg),0) as today_kg FROM sales_log WHERE type='resale' AND DATE(created_at)=CURDATE()")
        today_stats = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(qty_kg),0) as disposal_kg FROM sales_log WHERE type='disposal' AND DATE(created_at)=CURDATE()")
        disposal_stats = cur.fetchone()

        cur.execute("""
            SELECT r.recycled_id, r.recycled_name, r.unit, r.resale_price_per_kg, r.resale_price_per_count,
                c.category_name,
                COALESCE(i.stock_kg, 0) as stock_kg,
                COALESCE(i.stock_count, 0) as stock_count
            FROM recycled_products r
            JOIN waste_categories c ON r.category_id = c.category_id
            LEFT JOIN inventory i ON i.recycled_id = r.recycled_id
            ORDER BY r.recycled_name ASC
        """)
        products = cur.fetchall()
        cur.close()
        return render_template("staff_sales.html",
                            sales=sales, products=products,
                            total_rev=total_stats['total'], total_kg=total_stats['kg'], total_cnt=total_stats['cnt'],
                            today_rev=today_stats['today_rev'], today_kg=today_stats['today_kg'],
                            disposal_kg=disposal_stats['disposal_kg'],
                            search=search,
                            staff_name=session.get("staff_name","Staff"))

    @app.route("/add_sale", methods=["POST"])
    def add_sale():
        if "staff_id" not in session: return redirect(url_for("home"))
        try:
            recycled_id = request.form.get("recycled_id")
            sold_kg = float(request.form.get("sold_kg",0) or 0)
            sold_count = int(float(request.form.get("sold_count",0) or 0))
            rate_per_unit = float(request.form.get("rate_per_unit",0) or 0)
            company_name = request.form.get("company_name","").strip() or "N/A"
            buyer_name = request.form.get("buyer_details","").strip() or "N/A"
            buyer_contact = request.form.get("buyer_contact","").strip() or "0000000000"
            buyer_place = request.form.get("buyer_place","").strip() or "N/A"
            payment_mode = request.form.get("payment_mode","") or "Cash"
            vehicle_no = request.form.get("vehicle_no","") or "N/A"
            remarks = request.form.get("remarks","") or "N/A"

            if not recycled_id or (sold_kg <= 0 and sold_count <= 0):
                flash("Select product and enter quantity > 0", "error")
                return redirect(request.referrer or url_for("staff_sales"))

            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT stock_kg, stock_count, unit FROM inventory WHERE recycled_id=%s", (recycled_id,))
            inv = cur.fetchone()
            if not inv:
                flash("No stock - add inventory first", "error")
                cur.close()
                return redirect(request.referrer or url_for("staff_sales"))
            if sold_kg>0 and float(inv['stock_kg'] or 0) < sold_kg:
                flash(f"Only {inv['stock_kg']} kg available in inventory stock", "error")
                cur.close()
                return redirect(request.referrer or url_for("staff_sales"))
            if sold_count>0 and int(inv['stock_count'] or 0) < sold_count:
                flash(f"Only {inv['stock_count']} count available in inventory stock", "error")
                cur.close()
                return redirect(request.referrer or url_for("staff_sales"))
            qty = sold_kg if sold_kg>0 else sold_count
            base = qty * rate_per_unit
            gst_pct = float(request.form.get("gst_percent",0) or 0)
            total = base + (base * gst_pct/100)
            is_kg = (inv['unit'] == 'kg')
            if is_kg:
                cur.execute("""INSERT INTO sales_log (recycled_id, staff_id, type, qty_kg, qty_count, rate_per_kg, rate_per_count, amount, buyer_name, buyer_contact, buyer_company, buyer_place, payment_mode, gst_percent, vehicle_no, description, created_at)
            VALUES (%s,%s,'resale',%s,0,%s,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
            (recycled_id, session["staff_id"], sold_kg, rate_per_unit, total, buyer_name, buyer_contact, company_name, buyer_place, payment_mode, gst_pct, vehicle_no, remarks))
            else:
            # PCS / UNIT items - qty_kg = 0, qty_count = sold_count
                cur.execute("""INSERT INTO sales_log (recycled_id, staff_id, type, qty_kg, qty_count, rate_per_kg, rate_per_count, amount, buyer_name, buyer_contact, buyer_company, buyer_place, payment_mode, gst_percent, vehicle_no, description, created_at)
                            VALUES (%s,%s,'resale',0,%s,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                            (recycled_id, session["staff_id"], sold_count, rate_per_unit, total, buyer_name, buyer_contact, company_name, buyer_place, payment_mode, gst_pct, vehicle_no, remarks))
            new_kg = float(inv['stock_kg'] or 0) - sold_kg
            new_cnt = int(inv['stock_count'] or 0) - sold_count
            new_status = get_inventory_status(new_kg, new_cnt, inv['unit'])
            cur.execute("UPDATE inventory SET stock_kg=GREATEST(0,%s), stock_count=GREATEST(0,%s), status=%s, last_updated=NOW() WHERE recycled_id=%s", (new_kg, new_cnt, new_status, recycled_id))
            mysql.connection.commit()
            cur.close()
            flash(f"✅ SOLD to {company_name} - ₹{total:.2f} ({qty} {inv['unit']}) via {payment_mode}", "success")
        except Exception as e:
            print("add_sale ERROR", e)
            import traceback; traceback.print_exc()
            mysql.connection.rollback()
            flash(f"Sale error: {e}", "error")
        return redirect(request.referrer or url_for("staff_sales"))

    @app.route("/staff/edit_sale/<int:sale_id>", methods=["POST"])
    def edit_sale(sale_id):
        if "staff_id" not in session: return redirect(url_for("home"))
        try:
            sold_kg = float(request.form.get("sold_kg",0) or 0)
            sold_count = int(float(request.form.get("sold_count",0) or 0))
            rate_per_unit = float(request.form.get("rate_per_unit",0) or 0)
            company_name = request.form.get("company_name","").strip() or "N/A"
            buyer_name = request.form.get("buyer_details","").strip() or "N/A"
            buyer_contact = request.form.get("buyer_contact","").strip() or "0000000000"
            buyer_place = request.form.get("buyer_place","").strip() or "N/A"
            payment_mode = request.form.get("payment_mode","") or "Cash"
            vehicle_no = request.form.get("vehicle_no","") or "N/A"
            remarks = request.form.get("remarks","") or "N/A"
            gst_pct = float(request.form.get("gst_percent",0) or 0)

            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT * FROM sales_log WHERE id=%s", (sale_id,))
            sale = cur.fetchone()
            if not sale:
                flash("Sale record not found.", "error")
                cur.close()
                return redirect(url_for("staff_sales"))

            recycled_id = sale['recycled_id']
            old_kg = float(sale.get('qty_kg') or 0)
            old_count = int(sale.get('qty_count') or 0)

            cur.execute("SELECT stock_kg, stock_count, unit FROM inventory WHERE recycled_id=%s", (recycled_id,))
            inv = cur.fetchone()

            if inv:
                diff_kg = sold_kg - old_kg
                diff_count = sold_count - old_count
                current_stock_kg = float(inv['stock_kg'] or 0)
                current_stock_count = int(inv['stock_count'] or 0)
                if diff_kg > 0 and current_stock_kg < diff_kg:
                    flash(f"Insufficient inventory stock to increase sale weight! Only {current_stock_kg:.2f} kg additional available.", "error")
                    cur.close()
                    return redirect(url_for("staff_sales"))
                if diff_count > 0 and current_stock_count < diff_count:
                    flash(f"Insufficient inventory stock to increase sale count! Only {current_stock_count} pcs additional available.", "error")
                    cur.close()
                    return redirect(url_for("staff_sales"))
                new_stock_kg = max(0.0, current_stock_kg - diff_kg)
                new_stock_cnt = max(0, current_stock_count - diff_count)
                new_status = get_inventory_status(new_stock_kg, new_stock_cnt, inv['unit'])
                cur.execute("UPDATE inventory SET stock_kg=%s, stock_count=%s, status=%s, last_updated=NOW() WHERE recycled_id=%s",
                            (new_stock_kg, new_stock_cnt, new_status, recycled_id))

            qty = sold_kg if sold_kg > 0 else sold_count
            base = qty * rate_per_unit
            total = base + (base * gst_pct / 100)

            if sale['type'] == 'resale':
                cur.execute("""
                    UPDATE sales_log SET
                        qty_kg=%s, qty_count=%s, rate_per_kg=%s, rate_per_count=%s, amount=%s,
                        buyer_name=%s, buyer_contact=%s, buyer_company=%s, buyer_place=%s, payment_mode=%s,
                        gst_percent=%s, vehicle_no=%s, description=%s
                    WHERE id=%s
                """, (sold_kg, sold_count, rate_per_unit if sold_kg > 0 else 0, rate_per_unit if sold_count > 0 else 0,
                    total, buyer_name, buyer_contact, company_name, buyer_place, payment_mode, gst_pct, vehicle_no, remarks, sale_id))
            else:
                cur.execute("""
                    UPDATE sales_log SET
                        qty_kg=%s, qty_count=%s, amount=%s, buyer_company=%s, buyer_place=%s, description=%s
                    WHERE id=%s
                """, (sold_kg, sold_count, total, company_name, buyer_place, remarks, sale_id))

            mysql.connection.commit()
            cur.close()
            flash(f"✅ Sale transaction #{sale_id} updated successfully!", "success")
        except Exception as e:
            print("edit_sale error", e)
            import traceback; traceback.print_exc()
            mysql.connection.rollback()
            flash(f"Error updating sale: {e}", "error")
        return redirect(url_for("staff_sales"))                           

    @app.route("/add_disposal", methods=["POST"])
    def add_disposal():
        if "staff_id" not in session: return redirect(url_for("home"))
        try:
            category_id = request.form.get("category_id")
            qty_kg = float(request.form.get("qty_kg",0) or 0)
            reason = request.form.get("reason","Staff Disposal - Manual")
            if not category_id or qty_kg <= 0:
                flash("Select category and weight", "error")
                return redirect(request.referrer or url_for("all_disposals"))
            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT category_id, category_name, is_disposal FROM waste_categories WHERE category_id=%s", (category_id,))
            waste_cat = cur.fetchone()
            if not waste_cat:
                flash("Invalid waste category", "error")
                return redirect(request.referrer or url_for("all_disposals"))
            cat_name = waste_cat['category_name']
            if waste_cat['is_disposal'] == 1:
                cur.execute("SELECT recycled_id FROM recycled_products LIMIT 1")
                first = cur.fetchone()
                recycled_id = first['recycled_id'] if first else 1
            else:
                cur.execute("SELECT recycled_id FROM recycled_products WHERE category_id=%s LIMIT 1", (category_id,))
                prod = cur.fetchone()
                if prod:
                    recycled_id = prod['recycled_id']
                    cur.execute("SELECT stock_kg, stock_count, unit FROM inventory WHERE recycled_id=%s", (recycled_id,))
                    inv = cur.fetchone()
                    if inv:
                        new_kg = float(inv['stock_kg'] or 0) - qty_kg
                        new_status = get_inventory_status(new_kg, inv['stock_count'], inv['unit'])
                        cur.execute("UPDATE inventory SET stock_kg=GREATEST(0,%s), status=%s, last_updated=NOW() WHERE recycled_id=%s", (new_kg, new_status, recycled_id))
                    else:
                        cur.execute("UPDATE inventory SET stock_kg=GREATEST(0,stock_kg-%s), last_updated=NOW() WHERE recycled_id=%s", (qty_kg, recycled_id))
                else:
                    cur.execute("SELECT recycled_id FROM recycled_products LIMIT 1")
                    first = cur.fetchone()
                    recycled_id = first['recycled_id'] if first else 1
            cur.execute("""INSERT INTO sales_log (recycled_id, staff_id, type, qty_kg, qty_count, rate_per_kg, rate_per_count, amount, buyer_name, buyer_contact, buyer_company, payment_mode, gst_percent, vehicle_no, description, created_at)
            VALUES (%s,%s,'disposal',%s,0,0,0,0,%s,'0000000000','Municipality','N/A',0,'N/A',%s,NOW())""",
            (recycled_id, session["staff_id"], qty_kg, cat_name, reason))
            mysql.connection.commit()
            cur.close()
            flash(f"✅ Disposed {qty_kg} kg - {cat_name}", "success")
        except Exception as e:
            print(e); import traceback; traceback.print_exc(); mysql.connection.rollback()
            flash(f"Error: {e}", "error")
        return redirect(request.referrer or url_for("all_disposals"))

    @app.route("/recycling_dashboard")
    def recycling_dashboard():
        if "staff_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("""
            SELECT rp.recycled_id, rp.recycled_name, wc.category_name,
                rp.resale_price_per_kg, rp.unit,
                i.inventory_id, COALESCE(i.stock_kg,0) as stock_kg, COALESCE(i.stock_count,0) as stock_count, i.status
            FROM recycled_products rp
            JOIN waste_categories wc ON wc.category_id=rp.category_id
            LEFT JOIN inventory i ON i.recycled_id=rp.recycled_id
            ORDER BY rp.recycled_name ASC
        """)
        inventory = cur.fetchall()
        max_stock_kg = max([float(x['stock_kg'] or 0) for x in inventory]) if inventory else 1
        max_stock_count = max([int(x['stock_count'] or 0) for x in inventory]) if inventory else 1
        if max_stock_kg < 1: max_stock_kg = 1
        if max_stock_count < 1: max_stock_count = 1
        total_stock_kg = sum([float(x['stock_kg'] or 0) for x in inventory])
        total_stock_count = sum([int(x['stock_count'] or 0) for x in inventory])
        low_stock_items = [x for x in inventory if x.get('status')=='LOW']
        if not low_stock_items:
            for x in inventory:
                kg = float(x['stock_kg'] or 0)
                cnt = int(x['stock_count'] or 0)
                if x['unit'] == 'kg':
                    if 1 <= kg <= 5: low_stock_items.append(x)
                else:
                    if 1 <= cnt <= 5: low_stock_items.append(x)

        cur.execute("SELECT category_id, category_name FROM waste_categories WHERE is_disposal=1 ORDER BY category_name")
        disposal_categories = cur.fetchall()
        if not disposal_categories:
            cur.execute("SELECT category_id, category_name FROM waste_categories ORDER BY category_name")
            disposal_categories = cur.fetchall()

        try:
            cur.execute("""
                SELECT pr.created_at, wc.category_name, c.weight_kg, w.full_name, hu.area, hu.house_no
                FROM reward_logs c
                JOIN waste_categories wc ON wc.category_id=c.category_id
                JOIN pickup_requests pr ON pr.id=c.pickup_id
                JOIN household_users hu ON hu.household_id=c.household_id
                LEFT JOIN workers w ON w.worker_id=c.worker_id
                WHERE (wc.is_disposal=0 OR wc.is_disposal IS NULL) AND DATE(c.created_at)=CURDATE()
                ORDER BY c.created_at DESC LIMIT 100
            """)
            incoming = cur.fetchall()
        except:
            incoming = []

        try:
            cur.execute("""
                SELECT s.created_at, s.qty_kg, s.qty_count, s.amount,
                    s.buyer_name as category_name,
                    st.full_name as worker_name,
                    s.buyer_company as household_name, s.description as area,
                    s.staff_id as worker_id, s.description as reason
                FROM sales_log s
                LEFT JOIN staff st ON st.staff_id = s.staff_id
                WHERE s.type='disposal'
                ORDER BY s.created_at DESC LIMIT 100
            """)
            worker_disposals = cur.fetchall()
        except Exception as e:
            print("worker_disposals error", e)
            worker_disposals = []

        cur.execute("""
            SELECT s.type, COALESCE(rp.recycled_name, s.buyer_name, 'Disposal / Raw Waste') as category_name, s.qty_kg, s.qty_count, s.amount, s.created_at, s.description,
                COALESCE(st.full_name, CONCAT('Staff #', s.staff_id)) as staff_name
            FROM sales_log s
            LEFT JOIN recycled_products rp ON rp.recycled_id=s.recycled_id
            LEFT JOIN staff st ON st.staff_id=s.staff_id
            WHERE s.type='disposal' ORDER BY s.created_at DESC LIMIT 100
        """)
        logs = cur.fetchall()

        cur.execute("""
            SELECT s.created_at, COALESCE(rp.recycled_name, s.buyer_name, 'Disposal / Raw Waste') as category_name, s.qty_kg, s.qty_count, s.amount, s.description,
                s.buyer_name, s.buyer_company,
                COALESCE(st.full_name, CONCAT('Staff #', s.staff_id)) as staff_name
            FROM sales_log s
            LEFT JOIN recycled_products rp ON rp.recycled_id=s.recycled_id
            LEFT JOIN staff st ON st.staff_id=s.staff_id
            WHERE s.type='resale' ORDER BY s.created_at DESC LIMIT 50
        """)
        sale_logs = cur.fetchall()

        cur.execute("SELECT COALESCE(SUM(weight_kg),0) as total FROM reward_logs WHERE DATE(created_at)=CURDATE() AND category_id IN (SELECT category_id FROM waste_categories WHERE is_disposal=0 OR is_disposal IS NULL)")
        total_today = cur.fetchone()['total'] or 0

        cur.execute("SELECT COALESCE(SUM(weight_kg),0) as total FROM reward_logs WHERE category_id IN (SELECT category_id FROM waste_categories WHERE is_disposal=0 OR is_disposal IS NULL)")
        total_alltime = cur.fetchone()['total'] or 0

        cur.execute("SELECT COALESCE(SUM(qty_kg),0) as total FROM sales_log WHERE type='disposal' AND DATE(created_at)=CURDATE()")
        disposed_today = float(cur.fetchone()['total'] or 0)

        cur.execute("SELECT COALESCE(SUM(qty_kg),0) as total_kg FROM sales_log WHERE type='resale' AND DATE(created_at)=CURDATE()")
        sold_today = cur.fetchone()['total_kg'] or 0

        cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM sales_log WHERE DATE(created_at)=CURDATE()")
        revenue_today = cur.fetchone()['total'] or 0

        cur.execute("SELECT COALESCE(SUM(amount),0) as my_rev FROM sales_log WHERE staff_id=%s", (session["staff_id"],))
        my_revenue = cur.fetchone()['my_rev'] or 0

        cur.execute("SELECT COALESCE(SUM(amount),0) as all_rev FROM sales_log")
        all_staffs_revenue = cur.fetchone()['all_rev'] or 0
        total_revenue = all_staffs_revenue  # total revenue of all time

        try:
            cur.execute("""
                SELECT c.id, c.created_at, hu.full_name as household_name, hu.house_no, hu.ward_no, hu.area, 
                       w.full_name as worker_name, wc.category_name, c.weight_kg, c.points
                FROM reward_logs c
                JOIN household_users hu ON hu.household_id = c.household_id
                LEFT JOIN waste_categories wc ON wc.category_id = c.category_id
                LEFT JOIN workers w ON w.worker_id = c.worker_id
                ORDER BY c.created_at DESC
            """)
            all_collected_wastes = cur.fetchall()
        except Exception as e:
            print("all_collected_wastes error recycling", e)
            all_collected_wastes = []

        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS raw_waste_usage_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    category_id INT,
                    recycled_id INT,
                    staff_id INT,
                    kg_used DECIMAL(10,2),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        except: pass

        cur.execute("""
            SELECT wc.category_id, wc.category_name, wc.is_disposal,
                COALESCE((SELECT SUM(weight_kg) FROM reward_logs WHERE category_id=wc.category_id),0) as total_collected_kg,
                COALESCE((SELECT SUM(kg_used) FROM raw_waste_usage_log WHERE category_id=wc.category_id),0) as total_recycled_kg,
                COALESCE((SELECT SUM(qty_kg) FROM sales_log WHERE type='disposal' AND (recycled_id IN (SELECT recycled_id FROM recycled_products WHERE category_id=wc.category_id) OR buyer_name=wc.category_name OR description LIKE CONCAT('%%',wc.category_name,'%%'))),0) as total_disposed_kg
            FROM waste_categories wc
            ORDER BY wc.category_name ASC
        """)
        raw_waste_rows = cur.fetchall()
        raw_waste_stock = []
        for r in raw_waste_rows:
            collected = float(r['total_collected_kg'] or 0)
            recycled = float(r['total_recycled_kg'] or 0)
            disposed = float(r['total_disposed_kg'] or 0)
            current_available = max(0.0, round(collected - recycled - disposed, 2))
            raw_waste_stock.append({
                'category_id': r['category_id'],
                'category_name': r['category_name'],
                'total_collected_kg': round(collected, 2),
                'recycled_kg': round(recycled, 2),
                'disposed_kg': round(disposed, 2),
                'current_stock_kg': current_available
            })

        cur.close()

        return render_template("recycling_dashboard.html", all_collected_wastes=all_collected_wastes,
                            inventory=inventory,
                            raw_waste_stock=raw_waste_stock,
                            max_stock_kg=max_stock_kg,
                            max_stock_count=max_stock_count,
                            total_stock_kg=total_stock_kg,
                            total_stock_count=total_stock_count,
                            low_stock_items=low_stock_items,
                            disposal_categories=disposal_categories,
                            incoming=incoming,
                            worker_disposals=worker_disposals,
                            logs=logs,
                            sale_logs=sale_logs,
                            total_today=total_today,
                            total_alltime=total_alltime,
                            disposed_today=disposed_today,
                            sold_today=sold_today,
                            revenue_today=revenue_today,
                            my_revenue=my_revenue,
                            total_revenue=total_revenue,
                            all_staffs_revenue=all_staffs_revenue,
                            staff_name=session.get("staff_name","Staff"),
                            today_date=datetime.now().strftime("%d %b %Y"))

    @app.route("/all_disposals")
    def all_disposals():
        if "staff_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        category_id = request.args.get("category_id","")
        cur.execute("SELECT category_id, category_name FROM waste_categories ORDER BY category_name")
        disposal_categories = cur.fetchall()
        if category_id:
            cur.execute("SELECT category_name FROM waste_categories WHERE category_id=%s", (category_id,))
            cname_row = cur.fetchone()
            if cname_row:
                cname = cname_row['category_name']
                cur.execute("""
                    SELECT sl.id, sl.qty_kg, sl.description as reason, sl.created_at, sl.staff_id,
                           COALESCE(NULLIF(sl.buyer_name, 'Municipality'), wc.category_name, rp.recycled_name, sl.buyer_name, 'Disposal Waste') as category_name, 
                           COALESCE(st.full_name, CONCAT('Staff #', sl.staff_id)) as staff_name,
                           st.full_name as worker_name
                    FROM sales_log sl 
                    LEFT JOIN staff st ON st.staff_id=sl.staff_id
                    LEFT JOIN recycled_products rp ON rp.recycled_id=sl.recycled_id
                    LEFT JOIN waste_categories wc ON wc.category_id=rp.category_id
                    WHERE sl.type='disposal' AND (sl.buyer_name=%s OR sl.description LIKE %s OR wc.category_name=%s OR rp.recycled_name=%s)
                    ORDER BY sl.created_at DESC
                """, (cname, f"%{cname}%", cname, cname))
                logs = cur.fetchall()
            else:
                logs = []
        else:
            cur.execute("""
                SELECT sl.id, sl.qty_kg, sl.description as reason, sl.created_at, sl.staff_id,
                       COALESCE(NULLIF(sl.buyer_name, 'Municipality'), wc.category_name, rp.recycled_name, sl.buyer_name, 'Disposal Waste') as category_name, 
                       COALESCE(st.full_name, CONCAT('Staff #', sl.staff_id)) as staff_name,
                       st.full_name as worker_name
                FROM sales_log sl 
                LEFT JOIN staff st ON st.staff_id=sl.staff_id
                LEFT JOIN recycled_products rp ON rp.recycled_id=sl.recycled_id
                LEFT JOIN waste_categories wc ON wc.category_id=rp.category_id
                WHERE sl.type='disposal' 
                ORDER BY sl.created_at DESC
            """)
            logs = cur.fetchall()
        cur.close()
        return render_template("all_disposals.html", logs=logs, disposal_categories=disposal_categories, selected_category=category_id, staff_name=session.get("staff_name", "Staff"))

    @app.route("/staff/delete_disposal/<int:disposal_id>", methods=["POST", "GET"])
    def delete_disposal(disposal_id):
        if "staff_id" not in session: return redirect(url_for("home"))
        try:
            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT id, recycled_id, qty_kg, type FROM sales_log WHERE id=%s AND type='disposal'", (disposal_id,))
            disp = cur.fetchone()
            if not disp:
                flash("Disposal record not found.", "error")
                cur.close()
                return redirect(url_for("all_disposals"))
            recycled_id = disp['recycled_id']
            qty_kg = float(disp.get('qty_kg') or 0)
            if recycled_id and qty_kg > 0:
                cur.execute("SELECT stock_kg, stock_count, unit FROM inventory WHERE recycled_id=%s", (recycled_id,))
                inv = cur.fetchone()
                if inv:
                    new_kg = float(inv['stock_kg'] or 0) + qty_kg
                    new_status = get_inventory_status(new_kg, inv['stock_count'], inv['unit'])
                    cur.execute("UPDATE inventory SET stock_kg=%s, status=%s, last_updated=NOW() WHERE recycled_id=%s",
                                (new_kg, new_status, recycled_id))
            cur.execute("DELETE FROM sales_log WHERE id=%s", (disposal_id,))
            mysql.connection.commit()
            cur.close()
            flash(f"✅ Disposal record #{disposal_id} deleted successfully!", "success")
        except Exception as e:
            print("delete_disposal error", e)
            import traceback; traceback.print_exc()
            mysql.connection.rollback()
            flash(f"Error deleting disposal: {e}", "error")
        return redirect(url_for("all_disposals"))

    @app.route("/staff/delete_sale/<int:sale_id>", methods=["POST", "GET"])
    def delete_sale(sale_id):
        if "staff_id" not in session: return redirect(url_for("home"))
        try:
            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT recycled_id, qty_kg, qty_count, type FROM sales_log WHERE id=%s", (sale_id,))
            sale = cur.fetchone()
            if not sale:
                flash("Sale record not found.", "error")
                cur.close()
                return redirect(url_for("staff_sales"))

            recycled_id = sale['recycled_id']
            qty_kg = float(sale.get('qty_kg') or 0)
            qty_count = int(sale.get('qty_count') or 0)

            if recycled_id:
                cur.execute("SELECT stock_kg, stock_count, unit FROM inventory WHERE recycled_id=%s", (recycled_id,))
                inv = cur.fetchone()
                if inv:
                    new_kg = float(inv['stock_kg'] or 0) + qty_kg
                    new_count = int(inv['stock_count'] or 0) + qty_count
                    new_status = get_inventory_status(new_kg, new_count, inv['unit'])
                    cur.execute("UPDATE inventory SET stock_kg=%s, stock_count=%s, status=%s, last_updated=NOW() WHERE recycled_id=%s",
                                (new_kg, new_count, new_status, recycled_id))

            cur.execute("DELETE FROM sales_log WHERE id=%s", (sale_id,))
            mysql.connection.commit()
            cur.close()
            flash(f"✅ Sale transaction #{sale_id} deleted and stock restored!", "success")
        except Exception as e:
            print("delete_sale error", e)
            import traceback; traceback.print_exc()
            mysql.connection.rollback()
            flash(f"Error deleting sale: {e}", "error")
        return redirect(url_for("staff_sales"))

    @app.route("/staff_profile")
    def staff_profile():
        if "staff_id" not in session: return redirect(url_for("home"))
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM staff WHERE staff_id=%s", (session["staff_id"],))
        staff = cur.fetchone()
        cur.execute("SELECT COUNT(*) as cnt FROM sales_log WHERE staff_id=%s", (session["staff_id"],))
        total_sales = cur.fetchone()['cnt']
        cur.execute("SELECT COALESCE(SUM(qty_kg),0) as total_kg, COALESCE(SUM(amount),0) as total_rev FROM sales_log WHERE staff_id=%s", (session["staff_id"],))
        agg = cur.fetchone()
        sold_kg = agg['total_kg']; revenue = agg['total_rev']
        cur.execute("SELECT COALESCE(SUM(amount),0) as all_rev FROM sales_log")
        total_revenue_all = cur.fetchone()['all_rev'] or 0
        cur.close()
        return render_template("staff_profile.html", staff=staff, total_sales=total_sales, sold_kg=sold_kg, revenue=revenue, total_revenue_all=total_revenue_all)

    @app.route("/add_to_inventory", methods=["POST"])
    def add_to_inventory():
        if "staff_id" not in session: return redirect(url_for("home"))
        try:
            recycled_id = request.form.get("recycled_id")
            usable_kg = float(request.form.get("usable_kg", 0) or 0)
            usable_count = int(float(request.form.get("usable_count", 0) or 0))

            if not recycled_id or (usable_kg <= 0 and usable_count <= 0):
                flash("Please select a product and enter quantity > 0", "error")
                return redirect(url_for("recycling_dashboard"))

            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Get product name for flash message
            cur.execute("""
                SELECT rp.recycled_id, rp.recycled_name, rp.unit, wc.category_name
                FROM recycled_products rp
                JOIN waste_categories wc ON wc.category_id = rp.category_id
                WHERE rp.recycled_id = %s
            """, (recycled_id,))
            rp_row = cur.fetchone()
            if not rp_row:
                flash("Invalid product selected.", "error")
                cur.close()
                return redirect(url_for("recycling_dashboard"))

            prod_name = rp_row['recycled_name']
            unit = rp_row['unit']

            # Simply add to inventory — no raw waste stock check
            cur.execute("SELECT inventory_id, stock_kg, stock_count FROM inventory WHERE recycled_id=%s", (recycled_id,))
            row = cur.fetchone()
            if row:
                new_kg = float(row['stock_kg'] or 0) + usable_kg
                new_cnt = int(row['stock_count'] or 0) + usable_count
                new_status = get_inventory_status(new_kg, new_cnt, unit)
                cur.execute(
                    "UPDATE inventory SET stock_kg=%s, stock_count=%s, status=%s, last_updated=NOW() WHERE recycled_id=%s",
                    (new_kg, new_cnt, new_status, recycled_id)
                )
            else:
                new_status = get_inventory_status(usable_kg, usable_count, unit)
                cur.execute(
                    "INSERT INTO inventory (recycled_id, stock_kg, stock_count, unit, status) VALUES (%s,%s,%s,%s,%s)",
                    (recycled_id, usable_kg, usable_count, unit, new_status)
                )

            mysql.connection.commit()
            cur.close()
            qty_label = f"{usable_kg} kg" if unit == 'kg' else f"{usable_count} {unit}"
            flash(f"✅ Added {qty_label} of {prod_name} to inventory stock!", "success")
        except Exception as e:
            print("add_to_inventory error", e)
            import traceback; traceback.print_exc()
            mysql.connection.rollback()
            flash(f"Error: {e}", "error")
        return redirect(url_for("recycling_dashboard"))

    

    @app.route('/staff/deduct_raw_waste', methods=['POST'])
    def deduct_raw_waste():
        if "staff_id" not in session: return redirect(url_for("home"))
        try:
            cat_id = request.form.get("category_id")
            qty_kg = float(request.form.get("qty_kg", 0) or 0)
            deduct_type = request.form.get("deduct_type", "recycling")
            reason = request.form.get("reason", "").strip() or "Raw waste stock deduction"
            
            if qty_kg <= 0 or not cat_id:
                flash("Enter valid weight to deduct", "error")
                return redirect(url_for("recycling_dashboard"))

            cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT category_name FROM waste_categories WHERE category_id=%s", (cat_id,))
            cat = cur.fetchone()
            cat_name = cat['category_name'] if cat else "Waste"

            # Validate against available raw waste stock
            cur.execute("""
                SELECT 
                    COALESCE((SELECT SUM(weight_kg) FROM reward_logs WHERE category_id=%s),0) -
                    COALESCE((SELECT SUM(kg_used) FROM raw_waste_usage_log WHERE category_id=%s),0) -
                    COALESCE((SELECT SUM(qty_kg) FROM sales_log WHERE type='disposal' AND (recycled_id IN (SELECT recycled_id FROM recycled_products WHERE category_id=%s) OR buyer_name=%s OR description LIKE CONCAT('%%',%s,'%%'))),0) as available_stock
            """, (cat_id, cat_id, cat_id, cat_name, cat_name))
            stock_row = cur.fetchone()
            avail = float(stock_row['available_stock'] or 0) if stock_row else 0
            if qty_kg > avail:
                flash(f"Only {avail:.2f} kg of {cat_name} available in raw waste stock!", "error")
                cur.close()
                return redirect(url_for("recycling_dashboard"))

            if deduct_type == 'recycling':
                try:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS raw_waste_usage_log (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            category_id INT,
                            recycled_id INT,
                            staff_id INT,
                            kg_used DECIMAL(10,2),
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                except: pass
                
                cur.execute("INSERT INTO raw_waste_usage_log (category_id, staff_id, kg_used) VALUES (%s,%s,%s)",
                            (cat_id, session["staff_id"], qty_kg))
                mysql.connection.commit()
                flash(f"✅ Minused {qty_kg} kg from {cat_name} stock (Used for Recycling)", "success")

            else: # disposal
                cur.execute("SELECT recycled_id FROM recycled_products WHERE category_id=%s LIMIT 1", (cat_id,))
                prod = cur.fetchone()
                recycled_id = prod['recycled_id'] if prod else None
                if not recycled_id:
                    cur.execute("SELECT recycled_id FROM recycled_products LIMIT 1")
                    first = cur.fetchone()
                    recycled_id = first['recycled_id'] if first else 1

                cur.execute("""INSERT INTO sales_log (recycled_id, staff_id, type, qty_kg, qty_count, rate_per_kg, rate_per_count, amount, buyer_name, buyer_contact, buyer_company, payment_mode, gst_percent, vehicle_no, description, created_at)
                VALUES (%s,%s,'disposal',%s,0,0,0,0,%s,'0000000000','Municipality','N/A',0,'N/A',%s,NOW())""",
                (recycled_id, session["staff_id"], qty_kg, cat_name, reason))
                mysql.connection.commit()
                flash(f"✅ Minused {qty_kg} kg from {cat_name} stock (Disposed)", "success")
                
            cur.close()
        except Exception as e:
            print("deduct_raw_waste error", e)
            import traceback; traceback.print_exc()
            mysql.connection.rollback()
            flash(f"Error: {e}", "error")
        return redirect(url_for("recycling_dashboard"))

    from website.ai_chat import ai_chat_bp, init_mysql   # [AI-CHATBOT] remove this line to undo
    app.register_blueprint(ai_chat_bp); init_mysql(mysql) # [AI-CHATBOT] remove this line to undo
    return app

