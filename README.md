<img width="1920" height="1020" alt="Screenshot 2026-10-02 155204" src="https://github.com/user-attachments/assets/eb2366a2-06cd-4d9b-8a6a-826c0f501a65" /># ♻️ EcoCycle — Smart Waste Management Platform

EcoCycle is a full-stack web application that digitizes and streamlines municipal waste collection. It connects households, collection workers, recycling staff, and administrators on a single platform — with real-time pickup tracking, automated worker assignment, a rewards system, and recycling inventory management.

---

## 🚀 Features

### 👤 Multi-Role Authentication
- Four separate roles: **Admin**, **Household**, **Collection Worker**, and **Recycling Staff**
- Role-based dashboards and session management
- Household registration with Aadhaar document upload and admin approval flow
- Forgot password support across all roles

---

### 🏠 Household Portal
- Request waste pickup from the dashboard
- Track pickup status in real time (Pending → Assigned → Completed)
- Earn reward points for every kg of waste collected
- Redeem points directly to a UPI ID
- Submit feedback, complaints, or suggestions
- View full pickup history

---

### 🚛 Collection Worker Dashboard
- View and accept assigned pickup requests
- Mark pickups as Completed or "Not at Home"
- Live location update support
- View assigned households on a map/list
- Apply for leave with specific dates
- View personal pickup history

---

### 🔄 Smart Auto-Reassignment
When a worker goes on leave, EcoCycle **automatically reassigns** their pending pickups:
1. First looks for another worker in the **same ward + same area**
2. Falls back to any available worker in the **same ward**
3. Falls back to unassigned if no worker is available
- Reassignment logs are tracked and visible to admins
- Admins can also manually reassign any pickup to any worker

---

### ♻️ Recycling Staff Dashboard
- Log waste received by category and weight
- Add recycled products to inventory
- Record sales to buyers (with buyer details, payment mode, GST, vehicle number)
- Deduct raw waste stock when used for recycling or disposed
- View full recycling log and sales history

---

### 🛡️ Admin Control Panel
- **Live Dashboard** — today's pickup count, pending approvals, waste collected, worker performance
- **Ward Ledger** — per-ward breakdown of households, workers, pickups, and stuck requests
- **Today's Requests** — full view of all pickups with status, worker, weight, and timestamps
- **Worker Leaves** — approve/reject leave requests; auto-reassignment triggers on approval
- **Household Management** — approve/reject registrations, view Aadhaar documents
- **Feedback Management** — view and resolve complaints and suggestions
- **Waste Categories** — configure waste types and points per kg
- **Recycled Products** — manage what gets produced from recycled waste
- **Inventory** — track current stock levels with LOW/EMPTY alerts
- **Sales Log** — full record of recycled product sales with revenue tracking
- **Redeem Requests** — review and approve UPI payout requests from households
- **Festive Mode** — temporarily increase daily pickup limits during festivals

---

### 🎁 Reward System
- Households earn points based on waste weight × points-per-kg (configured per category)
- Points accumulate in a wallet
- Households can request redemption to their UPI ID at any time
- Admin reviews and approves payout requests
- Full redemption history tracked

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, Flask |
| Database | MySQL |
| Frontend | HTML, CSS, JavaScript, Jinja2 |
| Auth | Flask Sessions |
| File Uploads | Werkzeug |

---

## 📁 Project Structure

```
EcoCycle/
├── main.py                  # App entry point
├── website/
│   ├── __init__.py          # App factory, all routes
│   ├── views.py             # Additional views
│   ├── models.py            # DB models
│   ├── static/
│   │   ├── css/             # Stylesheets per page
│   │   ├── js/              # JS per page
│   │   └── uploads/         # Aadhaar & payment uploads
│   └── templates/           # Jinja2 HTML templates
│       ├── admin_dashboard.html
│       ├── household_dashboard.html
│       ├── worker_dashboard.html
│       ├── recycling_dashboard.html
│       └── ...
└── venv/                    # Python virtual environment
```

---

## ⚙️ Setup & Installation

### 1. Clone the repository
```bash
git clone https://github.com/yourusername/ecocycle.git
cd ecocycle
```

### 2. Create and activate virtual environment
```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Mac/Linux
source venv/bin/activate
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure the database
- Create a MySQL database named `ecocycle`
- Update credentials in `website/__init__.py`:
```python
app.config['MYSQL_HOST'] = '127.0.0.1'
app.config['MYSQL_USER'] = 'root'
app.config['MYSQL_PASSWORD'] = 'your_password'
app.config['MYSQL_DB'] = 'ecocycle'
```

### 5. Run the app
```bash
python main.py
```

Visit `http://127.0.0.1:5000` in your browser.

---

## 📸 Screenshots

> *(Add screenshots of Admin Dashboard, Household Dashboard, Worker Dashboard, and Rewards page here)*
<img width="1920" height="1020" alt="Screenshot 2026-10-02 155401" src="https://github.com/user-attachments/assets/aa87237a-efc7-4931-adcd-a383ee70f6e8" />
<img width="1920" height="1020" alt="Screenshot 2026-10-02 140134" src="https://github.com/user-attachments/assets/e04f4c4e-6cbf-4174-9209-08dd3a23442f" />
<img width="1920" height="1020" alt="Screenshot 2026-10-02 140205" src="https://github.com/user-attachments/assets/80a2f816-d6f7-4aee-b4f2-ee925d1b9dbe" />
<img width="1920" height="1020" alt="Screenshot 2026-10-02 140505" src="https://github.com/user-attachments/assets/5121d400-f87c-4f4f-875e-0534ffd34781" />
<img width="1920" height="1020" alt="Screenshot 2026-10-02 140533" src="https://github.com/user-attachments/assets/751ccba1-d2a6-4d1a-9646-18ef6907d613" />
<img width="1920" height="1020" alt="Screenshot 2026-10-02 140744" src="https://github.com/user-attachments/assets/a8a794fa-26c0-4ddb-a138-85b1bfdcf32b" />

---

## 🧑‍💻 Built With AI

This project was developed using **Claude** and **Cursor** as AI coding assistants — for writing complex SQL queries, generating boilerplate, debugging logic, and iterating on UI design rapidly.

---

## 📄 License

This project is for educational and demonstration purposes.

