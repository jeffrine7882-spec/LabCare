"""LabCare — sample data seeder for the live (InsForge Postgres) app.

Inserts a realistic demo picture: two customer organisations (tenant admins,
engineers, customer users, locations, departments, equipment), a spread of
complaints & breakdowns in various states, comments, preventive-maintenance
schedules and QR portal links. Uses the app's database layer so timestamps are
Malaysia time and the same helpers/checks apply.

Idempotent: exits without touching anything if any customer already exists.

Run:
    LABCARE_DATABASE_URL=postgresql://... python3 seed_sample.py
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import conn, now, now_dt, hash_password, backfill_audit_history

MASTER_EMAIL = "admin@labcare.com"
PASSWORD = "Demo123!"


def days_ago(n):
    return (now_dt() - timedelta(days=n, hours=now_dt().hour % 3)).strftime("%Y-%m-%d %H:%M:%S")


def seed_sample():
    c = conn()
    existing = c.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
    if existing:
        print(f"Sample data already present ({existing} customer(s)) — nothing to do.")
        c.close()
        return

    pwd = hash_password(PASSWORD)

    master = c.execute("SELECT id FROM users WHERE email=?", (MASTER_EMAIL,)).fetchone()
    master_id = master["id"] if master else 1

    # ---- customers -------------------------------------------------------
    customers = [
        ("BioReference Labs", "Dr. Kavita Nair", "kavita@bioref.com", "017-600 1111",
         "Level 3, Block A, Tech Park", "Kuala Lumpur"),
        ("Meridian Diagnostics", "Mr. Hasan Karim", "hasan@meridianlabs.com", "019-700 2222",
         "Unit 12, Jalan Industri", "Shah Alam"),
    ]
    cust_ids = []
    for name, contact, email, phone, address, city in customers:
        cur = c.execute(
            "INSERT INTO customers (name,contact_name,email,phone,address,city,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, contact, email, phone, address, city, days_ago(30)),
        )
        cust_ids.append(cur.lastrowid)
    bio_id, mer_id = cust_ids

    # ---- users -----------------------------------------------------------
    def add_user(name, email, role, customer_id, location_id=None, department_id=None,
                 phone="", ra_id=None):
        cur = c.execute(
            "INSERT INTO users (name,email,phone,password_hash,role,customer_id,"
            "location_id,department_id,active,responsible_admin_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,?,?)",
            (name, email, phone, pwd, role, customer_id, location_id, department_id,
             ra_id, days_ago(30)),
        )
        return cur.lastrowid

    bio_admin = add_user("BioReference Admin", "admin.bioref@labcare.com", "admin",
                         bio_id, phone="012-555 0110")
    mer_admin = add_user("Meridian Admin", "admin.meridian@labcare.com", "admin",
                         mer_id, phone="012-555 0111")
    aidil = add_user("Aidil Rahman", "aidil@labcare.com", "engineer", None,
                     phone="012-555 0101")
    meiling = add_user("Mei Ling Tan", "meiling@labcare.com", "engineer", None,
                       phone="012-555 0102")

    # ---- locations & departments -----------------------------------------
    def add_location(customer_id, name, address, city):
        cur = c.execute(
            "INSERT INTO locations (customer_id,name,address,city,created_at) VALUES (?,?,?,?,?)",
            (customer_id, name, address, city, days_ago(30)))
        return cur.lastrowid

    def add_department(customer_id, location_id, name):
        cur = c.execute(
            "INSERT INTO departments (customer_id,location_id,name,created_at) VALUES (?,?,?,?)",
            (customer_id, location_id, name, days_ago(30)))
        return cur.lastrowid

    bio_lab = add_location(bio_id, "Main Laboratory", "Level 3, Block A, Tech Park", "Kuala Lumpur")
    bio_cold = add_location(bio_id, "Cold Storage Facility", "Basement 1, Block B", "Kuala Lumpur")
    mer_plant = add_location(mer_id, "Analytical Plant", "Unit 12, Jalan Industri", "Shah Alam")
    bio_dept1 = add_department(bio_id, bio_lab, "Main Laboratory")
    bio_dept2 = add_department(bio_id, bio_cold, "Cold Storage Facility")
    mer_dept1 = add_department(mer_id, mer_plant, "Analytical Plant")
    mer_dept2 = mer_dept1

    # ---- customer users ---------------------------------------------------
    kavita = add_user("Dr. Kavita Nair", "kavita@bioref.com", "customer", bio_id,
                      bio_lab, bio_dept1, phone="017-600 1111", ra_id=bio_admin)
    hasan = add_user("Hasan Karim", "hasan@meridianlabs.com", "customer", mer_id,
                     mer_plant, mer_dept1, phone="019-700 2222", ra_id=mer_admin)

    # ---- equipment ---------------------------------------------------------
    def add_equipment(customer_id, location_id, department_id, name, model, serial, category,
                      installed, warranty, ra_id):
        cur = c.execute(
            "INSERT INTO equipment (customer_id,location_id,department_id,name,model,"
            "serial_number,category,installed_date,warranty_expiry,status,responsible_admin_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (customer_id, location_id, department_id, name, model, serial, category,
             installed, warranty, "active", ra_id, days_ago(28)),
        )
        return cur.lastrowid

    eq_centrifuge = add_equipment(bio_id, bio_lab, bio_dept1, "Centrifuge", "Eppendorf 5810 R",
                                  "EPP-5810R-001", "Centrifuges", "2023-04-12", "2026-04-11", bio_admin)
    eq_pcr = add_equipment(bio_id, bio_lab, bio_dept1, "Real-Time PCR System", "Bio-Rad CFX96",
                           "BR-CFX96-014", "PCR", "2022-08-03", "2025-08-02", bio_admin)
    eq_freezer = add_equipment(bio_id, bio_cold, bio_dept2, "Ultra-Low Freezer", "PHCbi MDF-U56VC",
                               "PHC-U56-221", "Cold Storage", "2021-11-20", "2024-11-19", bio_admin)
    eq_hplc = add_equipment(mer_id, mer_plant, mer_dept1, "HPLC System", "Agilent 1260 Infinity II",
                            "AGL-1260-098", "Chromatography", "2022-01-15", "2025-01-14", mer_admin)
    eq_autoclave = add_equipment(mer_id, mer_plant, mer_dept2, "Autoclave", "Tuttnauer 5075 ELV",
                                 "TUT-5075-033", "Sterilization", "2023-06-01", "2026-05-31", mer_admin)
    eq_spec = add_equipment(mer_id, mer_plant, mer_dept1, "Spectrophotometer", "Thermo Evolution 350",
                            "THE-EVO350-052", "Spectroscopy", "2020-03-30", "2023-03-29", mer_admin)

    # ---- complaints -------------------------------------------------------
    def add_complaint(code, customer_id, equipment_id, location_id, department_id, subject,
                      description, category, priority, status, created_by, assigned_to,
                      created_n_days, resolved_n_days=None, accepted_by=None, accept_reply=""):
        created = days_ago(created_n_days)
        resolved = days_ago(resolved_n_days) if resolved_n_days else None
        closed_by = assigned_to if status in ("resolved", "closed") and resolved else None
        cur = c.execute(
            "INSERT INTO complaints (code,customer_id,equipment_id,location_id,department_id,"
            "subject,description,category,priority,status,created_by,assigned_to,created_at,"
            "updated_at,resolved_at,closed_by,accepted_by,accepted_at,accept_reply,responsible_admin_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, customer_id, equipment_id, location_id, department_id, subject, description,
             category, priority, status, created_by, assigned_to, created, created,
             resolved, closed_by, accepted_by,
             days_ago(max(1, created_n_days - 1)) if accepted_by else None,
             accept_reply, bio_admin if customer_id == bio_id else mer_admin),
        )
        return cur.lastrowid

    cmp1 = add_complaint("CMP-0001", bio_id, eq_freezer, bio_cold, bio_dept2,
                         "Freezer temperature alarm keeps triggering",
                         "The -80°C freezer triggers a temperature alarm several times a day. "
                         "Samples include clinical trial material, so this is urgent.",
                         "Cold Storage", "critical", "in_progress", kavita, aidil, 4,
                         accepted_by=aidil, accept_reply="On it — inspection scheduled tomorrow 10:00 AM.")
    cmp2 = add_complaint("CMP-0002", mer_id, eq_hplc, mer_plant, mer_dept1,
                         "HPLC baseline drift on detector",
                         "Baseline drifts during gradient runs. Performed a routine flush but the issue persists.",
                         "Chromatography", "high", "open", hasan, meiling, 2)
    cmp3 = add_complaint("CMP-0003", bio_id, eq_centrifuge, bio_lab, bio_dept1,
                         "Centrifuge vibrating unusually",
                         "Noticeable vibration and noise at 10,000 rpm. Rotor was balanced correctly.",
                         "Centrifuges", "medium", "resolved", kavita, aidil, 9, resolved_n_days=6)
    add_complaint("CMP-0004", mer_id, eq_spec, mer_plant, mer_dept1,
                  "Spectrophotometer lamp error",
                  "Recurring 'lamp aged' error. Request check and lamp replacement estimate.",
                  "Spectroscopy", "medium", "closed", hasan, meiling, 14, resolved_n_days=11)

    # ---- breakdowns --------------------------------------------------------
    def add_breakdown(code, equipment_id, customer_id, complaint_id, location_id, department_id,
                      fault, root_cause, priority, status, reported_by, assigned_to,
                      created_n_days, resolution="", resolved_n_days=None):
        created = days_ago(created_n_days)
        resolved = days_ago(resolved_n_days) if resolved_n_days else None
        closed_by = assigned_to if status == "resolved" and resolved else None
        cur = c.execute(
            "INSERT INTO breakdowns (code,equipment_id,customer_id,complaint_id,location_id,"
            "department_id,fault_description,root_cause,priority,status,reported_by,assigned_to,"
            "resolution_notes,created_at,updated_at,resolved_at,closed_by,responsible_admin_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, equipment_id, customer_id, complaint_id, location_id, department_id,
             fault, root_cause, priority, status, reported_by, assigned_to, resolution,
             created, created, resolved, closed_by,
             bio_admin if customer_id == bio_id else mer_admin),
        )
        return cur.lastrowid

    brk1 = add_breakdown("BRK-0001", eq_freezer, bio_id, cmp1, bio_cold, bio_dept2,
                         "Door seal leaking — frost build-up and door not sealing fully. "
                         "Compressor running continuously.",
                         "Worn magnetic door gasket; hinge misalignment.",
                         "critical", "diagnosed", kavita, aidil, 3)
    brk2 = add_breakdown("BRK-0002", eq_hplc, mer_id, None, mer_plant, mer_dept1,
                         "Detector baseline drift; lamp intensity fluctuating in diagnostics.",
                         "", "high", "in_progress", hasan, meiling, 1)
    add_breakdown("BRK-0003", eq_autoclave, mer_id, None, mer_plant, mer_dept2,
                  "Pressure gauge not reaching sterilisation temperature.",
                  "Heater element degraded.", "medium", "resolved", hasan, meiling, 8,
                  resolution="Replaced heater element and validated a full cycle.", resolved_n_days=5)

    # ---- comments -----------------------------------------------------------
    c.execute("INSERT INTO comments (entity_type,entity_id,user_id,text,created_at) VALUES (?,?,?,?,?)",
              ("complaint", cmp1, aidil, "Inspection scheduled tomorrow 10:00 AM. Please keep the freezer contents logged.", days_ago(3)))
    c.execute("INSERT INTO comments (entity_type,entity_id,user_id,text,created_at) VALUES (?,?,?,?,?)",
              ("complaint", cmp1, kavita, "Understood. Clinical trial samples are logged in the temperature chart. Thank you.", days_ago(3)))
    c.execute("INSERT INTO comments (entity_type,entity_id,user_id,text,created_at) VALUES (?,?,?,?,?)",
              ("breakdown", brk1, kavita, "Please prioritise — these are patient-critical samples.", days_ago(2)))

    # ---- preventive maintenance ---------------------------------------------
    def add_pm(customer_id, equipment_id, title, description, interval_days, last_done,
               next_due, assigned_to):
        cur = c.execute(
            "INSERT INTO pm_schedules (customer_id,equipment_id,title,description,interval_days,"
            "last_done_at,next_due_at,assigned_to,active,created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
            (customer_id, equipment_id, title, description, interval_days, last_done, next_due,
             assigned_to, days_ago(20)),
        )
        return cur.lastrowid

    add_pm(bio_id, eq_freezer, "Ultra-low freezer PM",
           "Defrost cycle, door gasket check, compressor & alarm verification.", 180,
           days_ago(200), days_ago(20), aidil)  # overdue -> shows in "PM due"
    add_pm(bio_id, eq_centrifuge, "Centrifuge annual service",
           "Rotor inspection, lid lock check, brake test and recalibration.", 365,
           days_ago(360), days_ago(-5), aidil)  # due in a few days
    add_pm(mer_id, eq_hplc, "HPLC PM kit", "Pump seals, lamp energy check and column flush.", 120,
           days_ago(120), days_ago(-7), meiling)

    # ---- QR portal links -------------------------------------------------------
    c.execute(
        "INSERT INTO portal_links (token,customer_id,equipment_id,label,active,created_by,created_at) "
        "VALUES (?,?,?,?,1,?,?)",
        ("brf-freezer", bio_id, eq_freezer, "Freezer QR — BioReference", master_id, days_ago(25)))
    c.execute(
        "INSERT INTO portal_links (token,customer_id,equipment_id,label,active,created_by,created_at) "
        "VALUES (?,?,?,?,1,?,?)",
        ("mdx-hplc", mer_id, eq_hplc, "HPLC QR — Meridian", master_id, days_ago(25)))

    # ---- notifications (so the bell has content) ------------------------------
    def add_notification(user_id, entity_type, entity_id, text, n_days, read=0):
        c.execute(
            "INSERT INTO notifications (user_id,entity_type,entity_id,text,read,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, entity_type, entity_id, text, read, days_ago(n_days)))
        c.execute(
            "INSERT INTO notification_pings (user_id,updated_at) VALUES (?,?) "
            "ON CONFLICT (user_id) DO UPDATE SET updated_at=excluded.updated_at",
            (user_id, days_ago(n_days)))

    add_notification(bio_admin, "complaint", cmp1,
                     "New complaint CMP-0001 by Dr. Kavita Nair: Freezer temperature alarm keeps triggering", 4)
    add_notification(mer_admin, "complaint", cmp2,
                     "New complaint CMP-0002 by Hasan Karim: HPLC baseline drift on detector", 2)
    add_notification(aidil, "complaint", cmp1, "CMP-0001 assigned to you (Aidil Rahman)", 4, read=1)

    # The master admin also gets a bell roll-up of activity across the estate.
    add_notification(master_id, "complaint", cmp1,
                     "New complaint CMP-0001 by Dr. Kavita Nair (BioReference Labs): Freezer temperature alarm keeps triggering", 4)
    add_notification(master_id, "breakdown", brk1,
                     "Breakdown BRK-0001 reported on Ultra-Low Freezer (BioReference Labs)", 3)
    add_notification(master_id, "complaint", cmp2,
                     "New complaint CMP-0002 by Hasan Karim (Meridian Diagnostics): HPLC baseline drift on detector", 2)
    add_notification(master_id, "complaint", cmp3,
                     "Complaint CMP-0003 resolved: Centrifuge vibrating unusually", 9, read=1)
    c.execute(
        "INSERT INTO notification_pings (user_id,updated_at) VALUES (?,?) "
        "ON CONFLICT (user_id) DO UPDATE SET updated_at=excluded.updated_at",
        (master_id, days_ago(15)))

    # ---- audit backfill (opened/assigned/status/resolution history) ------------
    backfill_audit_history(c)

    c.commit()
    c.close()
    print("Sample data seeded:")
    print(f"  customers: {len(cust_ids)}  locations: 3  departments: 4  equipment: 6")
    print(f"  users: master + 2 tenant admins + 2 engineers + 2 customer users")
    print(f"  complaints: 4 (open/in_progress/resolved/closed)  breakdowns: 3")
    print(f"  comments: 3  PM schedules: 3  portal links: 2  notifications: 7 (incl. master bell)")
    print(f"  All passwords: {PASSWORD}")


if __name__ == "__main__":
    if not os.environ.get("LABCARE_DATABASE_URL") and not os.environ.get("DATABASE_URL"):
        print("Set LABCARE_DATABASE_URL to the InsForge Postgres connection string first.")
        sys.exit(2)
    seed_sample()
