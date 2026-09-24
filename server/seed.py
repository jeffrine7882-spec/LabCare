"""LabCare — seed data (demo customers, locations, departments, equipment,
complaints, breakdowns, users)."""
from database import conn, now, hash_password, init_db


def seed():
    init_db()
    c = conn()

    # Users — start fresh session table each seed
    c.execute("DELETE FROM sessions")
    existing = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if existing:
        c.commit()
        c.close()
        return

    password = "Demo123!"
    pwd = hash_password(password)

    # Customers are split into locations and departments.
    # Customer-role users are bound to a single location + department and can
    # only see / create tickets & equipment within that scope.
    users = [
        # (name, email, phone, password_hash, role, customer_id, location_id, department_id)
        ("System Admin", "admin@labcare.com", "012-555 0100", pwd, "admin", None, None, None),
        ("Aidil Rahman", "aidil@labcare.com", "012-555 0101", pwd, "technician", None, None, None),
        ("Mei Ling Tan", "meiling@labcare.com", "012-555 0102", pwd, "technician", None, None, None),
        ("Support Desk", "support@labcare.com", "012-555 0103", pwd, "technician", None, None, None),
        # BioReference Labs (customer 1) — two users in *different* locations/depts
        ("Dr. Kavita Nair", "kavita@bioref.com", "017-600 1111", pwd, "customer", 1, 1, 1),
        ("Sarah Chong", "sarah@bioref.com", "017-600 1112", pwd, "customer", 1, 2, 3),
        # Meridian Diagnostics (customer 2)
        ("Hasan Karim", "hasan@meridianlabs.com", "019-700 2222", pwd, "customer", 2, 3, 4),
        # Northern General Hospital (customer 3)
        ("Grace Wong", "grace@northernhospital.my", "016-800 3333", pwd, "customer", 3, 4, 6),
    ]
    c.executemany(
        "INSERT INTO users (name,email,phone,password_hash,role,customer_id,location_id,department_id,active,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,?)",
        [u + (now(),) for u in users],
    )

    customers = [
        ("BioReference Labs", "Dr. Kavita Nair", "kavita@bioref.com", "017-600 1111", "Level 3, Block A, Tech Park", "Kuala Lumpur"),
        ("Meridian Diagnostics", "Hasan Karim", "hasan@meridianlabs.com", "019-700 2222", "Unit 12, Jalan Industri", "Shah Alam"),
        ("Northern General Hospital", "Grace Wong", "grace@northernhospital.my", "016-800 3333", "Jalan Hospital", "Penang"),
    ]

    # Default equipment categories (admin can add/edit/delete them in-app)
    categories = [
        "Centrifuges", "PCR", "Cold Storage", "Chromatography", "Spectroscopy",
        "Sterilization", "Analyzers", "Histology", "Microscopy", "Other",
    ]
    c.executemany(
        "INSERT OR IGNORE INTO categories (name, created_at) VALUES (?,?)",
        [(x, now()) for x in categories],
    )
    c.executemany(
        "INSERT INTO customers (name,contact_name,email,phone,address,city,created_at) VALUES (?,?,?,?,?,?,?)",
        [cust + (now(),) for cust in customers],
    )

    # Locations (per customer)
    locations = [
        (1, "Main Laboratory", "Level 3, Block A, Tech Park", "Kuala Lumpur"),
        (1, "Cold Storage Facility", "Basement 1, Block B", "Kuala Lumpur"),
        (2, "Analytical Plant", "Unit 12, Jalan Industri", "Shah Alam"),
        (3, "Hospital Block A", "Jalan Hospital", "Penang"),
    ]
    c.executemany(
        "INSERT INTO locations (customer_id,name,address,city,created_at) VALUES (?,?,?,?,?)",
        [l + (now(),) for l in locations],
    )

    # Departments (per location)
    departments = [
        (1, 1, "Molecular Diagnostics"),
        (1, 1, "Sample Processing"),
        (1, 2, "Biobank"),
        (2, 3, "Analytical Chemistry"),
        (2, 3, "Quality Control"),
        (3, 4, "ICU Laboratory"),
        (3, 4, "Pathology"),
    ]
    c.executemany(
        "INSERT INTO departments (customer_id,location_id,name,created_at) VALUES (?,?,?,?)",
        [d + (now(),) for d in departments],
    )

    equipment = [
        # (customer_id, location_id, department_id, name, model, serial, category, installed, warranty, status, notes)
        # customer 1 — Main Laboratory / Molecular Diagnostics
        (1, 1, 1, "Centrifuge", "Eppendorf 5810 R", "EPP-5810R-001", "Centrifuges", "2023-04-12", "2026-04-11", "active", ""),
        (1, 1, 1, "Real-Time PCR System", "Bio-Rad CFX96", "BR-CFX96-014", "PCR", "2022-08-03", "2025-08-02", "active", ""),
        # customer 1 — Cold Storage / Biobank
        (1, 2, 3, "Ultra-Low Freezer", "PHCbi MDF-U56VC", "PHC-U56-221", "Cold Storage", "2021-11-20", "2024-11-19", "active", "Frequent ice build-up on door seal"),
        # customer 2 — Analytical Plant / Analytical Chemistry
        (2, 3, 4, "HPLC System", "Agilent 1260 Infinity II", "AGL-1260-098", "Chromatography", "2022-01-15", "2025-01-14", "active", ""),
        (2, 3, 4, "Spectrophotometer", "Thermo Evolution 350", "THE-EVO350-052", "Spectroscopy", "2020-03-30", "2023-03-29", "active", ""),
        # customer 2 — Analytical Plant / Quality Control
        (2, 3, 5, "Autoclave", "Tuttnauer 5075 ELV", "TUT-5075-033", "Sterilization", "2023-06-01", "2026-05-31", "active", ""),
        # customer 3 — Hospital Block A / ICU Laboratory
        (3, 4, 6, "Blood Gas Analyzer", "Siemens RAPIDPoint 500", "SIE-RP500-077", "Analyzers", "2022-10-05", "2025-10-04", "active", ""),
        # customer 3 — Hospital Block A / Pathology
        (3, 4, 7, "Microtome", "Leica RM2255", "LEI-RM2255-019", "Histology", "2019-07-08", "2022-07-07", "active", ""),
    ]
    c.executemany(
        "INSERT INTO equipment (customer_id,location_id,department_id,name,model,serial_number,category,installed_date,warranty_expiry,status,notes,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [e + (now(),) for e in equipment],
    )

    complaints = [
        # (code, customer_id, equipment_id, location_id, department_id, subject, description, category,
        #  priority, status, created_by, assigned_to, created_at, resolved_at)
        ("CMP-0001", 1, 3, 2, 3, "Freezer temperature alarm keeps triggering",
         "The -80°C freezer triggers a temperature alarm several times a day. Samples stored include clinical trial material, so this is urgent.",
         "Cold Storage", "critical", "in_progress", 6, 2, "2026-09-15 09:12:00", None),
        ("CMP-0002", 2, 4, 3, 4, "HPLC baseline drift on detector",
         "Baseline drifts during gradient runs. Performed routine flush but the issue persists.",
         "Chromatography", "high", "open", 7, 3, "2026-09-17 14:40:00", None),
        ("CMP-0003", 3, 7, 4, 6, "Blood gas analyzer slow startup",
         "Unit takes more than 30 minutes to become ready. Nursing staff affected during morning rounds.",
         "Analyzers", "high", "resolved", 8, 4, "2026-09-10 08:05:00", "2026-09-13 16:20:00"),
        ("CMP-0004", 1, 1, 1, 1, "Centrifuge vibrating unusually",
         "Noticeable vibration and noise at 10,000 rpm. Rotor was balanced correctly.",
         "Centrifuges", "medium", "closed", 5, 2, "2026-08-28 11:30:00", "2026-09-02 12:00:00"),
        ("CMP-0005", 2, 5, 3, 4, "Spectrophotometer lamp error",
         "Recurring 'lamp aged' error. Request check and lamp replacement estimate.",
         "Spectroscopy", "medium", "open", 7, 4, "2026-09-18 10:15:00", None),
    ]
    c.executemany(
        "INSERT INTO complaints (code,customer_id,equipment_id,location_id,department_id,subject,description,category,priority,status,created_by,assigned_to,created_at,updated_at,resolved_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(cc[0], cc[1], cc[2], cc[3], cc[4], cc[5], cc[6], cc[7], cc[8], cc[9], cc[10], cc[11], cc[12], cc[12], cc[13]) for cc in complaints],
    )

    breakdowns = [
        # (code, equipment_id, customer_id, complaint_id, location_id, department_id, fault, root_cause,
        #  priority, status, reported_by, assigned_to, resolution, created_at, resolved_at)
        ("BRK-0001", 3, 1, 1, 2, 3, "Door seal leaking — frost build-up and door not sealing fully. Compressor running continuously.",
         "Worn magnetic door gasket; hinge misalignment.",
         "critical", "diagnosed", 6, 2, "", "2026-09-15 09:40:00", None),
        ("BRK-0002", 4, 2, 2, 3, 4, "Detector baseline drift; lamp intensity fluctuating in diagnostics.",
         "", "high", "in_progress", 7, 3, "", "2026-09-17 15:10:00", None),
        ("BRK-0003", 7, 3, 3, 4, 6, "Internal battery degraded; startup self-test timing out.",
         "Maintenance recommended every 12 months was overdue; battery replaced.",
         "high", "resolved", 8, 4, "Replaced internal battery and recalibrated. Advised on 12-month PM schedule.",
         "2026-09-10 08:40:00", "2026-09-13 15:30:00"),
        ("BRK-0004", 1, 1, 4, 1, 1, "Worn drive belt causing vibration at high rpm.",
         "Belt replaced and rotor rebalanced.",
         "medium", "resolved", 5, 2, "Replaced drive belt; vibration levels normal on test run.",
         "2026-08-28 11:50:00", "2026-09-01 10:30:00"),
        ("BRK-0005", 5, 2, 5, 3, 4, "UV lamp reached end of life; absorbance readings unstable.",
         "", "medium", "reported", 7, 4, "", "2026-09-18 10:20:00", None),
    ]
    c.executemany(
        "INSERT INTO breakdowns (code,equipment_id,customer_id,complaint_id,location_id,department_id,fault_description,root_cause,priority,status,reported_by,assigned_to,resolution_notes,created_at,updated_at,resolved_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13], b[13], b[14]) for b in breakdowns],
    )

    comments = [
        ("complaint", 1, 2, now(), "Inspection scheduled tomorrow 10:00 AM. Please keep the freezer contents logged."),
        ("complaint", 1, 6, now(), "Understood. Clinical trial samples are logged in the temperature chart. Thank you."),
        ("breakdown", 1, 6, now(), "Please prioritise — these are patient-critical samples."),
        ("complaint", 3, 4, now(), "Part replaced. Closing the ticket. PM contract renewal advised."),
    ]
    c.executemany(
        "INSERT INTO comments (entity_type,entity_id,user_id,created_at,text) VALUES (?,?,?,?,?)",
        comments,
    )

    # Preventive maintenance schedules
    pms = [
        # (customer_id, equipment_id, title, description, interval_days, last_done_at, next_due_at, assigned_to)
        (1, 1, "Centrifuge annual service", "Rotor inspection, lid lock check, brake test and recalibration.", 365,
         "2025-04-10 00:00:00", "2026-04-10 00:00:00", 2),
        (1, 3, "Ultra-low freezer PM", "Defrost cycle, door gasket check, compressor & alarm verification.", 180,
         "2026-05-20 00:00:00", "2026-11-16 00:00:00", 2),
        (1, 2, "PCR calibration", "Optical calibration and thermal uniformity verification.", 180,
         "2026-03-01 00:00:00", "2026-09-18 00:00:00", 3),
        (2, 4, "HPLC PM kit", "Pump seals, lamp energy check and column flush.", 120,
         "2026-06-01 00:00:00", "2026-09-29 00:00:00", 3),
        (2, 6, "Autoclave safety check", "Pressure vessel inspection, gasket & safety valve test.", 365,
         "2025-07-15 00:00:00", "2026-07-15 00:00:00", 4),
        (3, 7, "Blood gas analyser PM", "Probe cleaning, QC cartridge and battery check.", 60,
         "2026-08-25 00:00:00", "2026-10-24 00:00:00", 2),
    ]
    c.executemany(
        "INSERT INTO pm_schedules (customer_id,equipment_id,title,description,interval_days,last_done_at,next_due_at,assigned_to,active,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,?)",
        [p + (now(),) for p in pms],
    )

    # PM history logs
    pm_logs = [
        (1, 1, 2, "2025-04-10 09:30:00", "Rotor balanced; brake test passed; recertified."),
        (3, 7, 2, "2026-08-25 14:00:00", "Battery replaced, QC cartridge renewed."),
        (2, 1, 3, "2026-03-01 10:15:00", "Optical calibration within spec."),
    ]
    c.executemany(
        "INSERT INTO pm_logs (schedule_id,equipment_id,performed_by,performed_at,notes) VALUES (?,?,?,?,?)",
        pm_logs,
    )

    # Customer portal QR links
    # (token, customer_id, equipment_id, label, active, created_at)
    portals = [
        ("brf-freezer", 1, 3, "Freezer QR — BioReference", 1, "2026-09-01 10:00:00"),
        ("mdx-hplc", 2, 4, "HPLC QR — Meridian", 1, "2026-09-02 11:00:00"),
    ]
    c.executemany(
        "INSERT INTO portal_links (token,customer_id,equipment_id,label,active,created_by,created_at) VALUES (?,?,?,?,?,1,?)",
        portals,
    )

    # Reconstruct opening/assignment/status/resolution history for the freshly
    # seeded tickets so the per-ticket activity log is populated on first run.
    from database import backfill_audit_history
    backfill_audit_history(c)

    c.commit()
    c.close()
    print("Seed complete: locations, departments, users, customers, equipment, complaints, breakdowns created.")
