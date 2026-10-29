"""Synthetic vet-clinic schema, planted constants, and sample questions.

No database connection and no Faker import. Supplier lead time and minimum
order quantity live here as seeded column values. Appointment types and usage
rows are seeded constants, the same for every seed.
"""

from dataclasses import dataclass

DATA_CLASS = "synthetic"
LABEL = "synthetic"
NOTICE = (
    "SYNTHETIC VET-CLINIC SIMULATOR. NOT A CLINIC RECORD. "
    "NO REAL PATIENT, CLIENT, OR STAFF DATA."
)

FILLER_CLINICIANS = 4
FILLER_OWNERS = 24
FILLER_PATIENTS = 36
FILLER_STEP_SECONDS = 86400
APPOINTMENT_SECONDS = 1800
LOT_LIFE_DAYS = 400
PLANTED_ID_FLOOR = 9000
CHECKUP_APPOINTMENT_TYPE_ID = 1
FILLER_CLINICIAN_ROLE = "synthetic veterinarian"
LINE_DESCRIPTION = "synthetic line"
FILLER_INVOICE_AMOUNTS = (1000, 2500)
FILLER_INVOICE_TOTAL = 3500
FILLER_INVOICE_STATUS = "open"
FILLER_APPOINTMENT_STATUS = "booked"

SPECIES = ("dog", "cat", "rabbit")
BREEDS = ("SYN-DOG", "SYN-CAT", "SYN-RABBIT")
CITIES = (
    "North Synthetic",
    "West Synthetic",
    "East Synthetic",
    "South Synthetic",
)
PET_NAMES = (
    "Amberbit",
    "Bramblehop",
    "Cindertail",
    "Driftpaw",
    "Emberwhisk",
    "Fennelfoot",
    "Glimmerear",
    "Harborcoat",
    "Indigopaw",
    "Juniperbit",
    "Kestrelfur",
    "Lumenhop",
)
WEIGHT_GRAMS = {
    "dog": (2000, 60000),
    "cat": (500, 8000),
    "rabbit": (200, 4000),
}

# (product_id, qty) rows usage must return, in usage_id order.
EXPECTED_USAGE = {
    1: ((1, 1),),
    9001: ((9001, 1),),
    9002: ((9003, 1),),
}
PLANTED_VISIT_LOT = {9001: 9006, 9002: 9001, 9003: 9002, 9004: 9005}
# (stock_use_id, visit_id). Product and quantity come from usage.
PLANTED_STOCK_DRAW = (
    (9001, 9002),
    (9002, 9003),
    (9003, 9004),
    (9004, 9001),
)

# Column order matches CREATE TABLE. The generator inserts by position.
PLANTED_CLINICIANS = ((9001, "Synthetic Clinician 9001", "synthetic veterinarian"),)
PLANTED_OWNERS = (
    (
        9001,
        "Synthetic Owner 9001",
        "owner9001@example.com",
        "+1-555-0199",
        "North Synthetic",
    ),
)
PLANTED_PATIENTS = (
    (
        9001,
        9001,
        "Synthetic Pet 9001",
        "dog",
        "SYN-DOG",
        "F",
        "2019-01-01",
        "SYN-CHIP-9001",
        "deceased",
        "2024-03-01",
    ),
    (
        9002,
        9001,
        "Synthetic Pet 9002",
        "dog",
        "SYN-DOG",
        "M",
        "2019-02-02",
        "SYN-CHIP-9000",
        "alive",
        None,
    ),
    (
        9003,
        9001,
        "Synthetic Pet 9003",
        "dog",
        "SYN-DOG",
        "F",
        "2019-03-03",
        "SYN-CHIP-9000",
        "alive",
        None,
    ),
    (
        9004,
        9001,
        "Synthetic Pet 9004",
        "cat",
        "SYN-CAT",
        "F",
        "2020-04-04",
        "SYN-CHIP-9004",
        "alive",
        None,
    ),
    (
        9005,
        9001,
        "Synthetic Pet 9005",
        "dog",
        "SYN-DOG",
        "M",
        "2019-05-05",
        "SYN-CHIP-9005",
        "alive",
        None,
    ),
)
PLANTED_APPOINTMENT_TYPES = (
    (1, "checkup"),
    (9001, "vaccine"),
    (9002, "overorder"),
)
PLANTED_APPOINTMENTS = (
    (
        9001,
        9001,
        9001,
        1,
        "2024-04-01T15:00:00Z",
        "2024-04-01T15:30:00Z",
        "booked",
    ),
    (
        9002,
        9002,
        9001,
        1,
        "2024-05-02T15:00:00Z",
        "2024-05-02T14:00:00Z",
        "booked",
    ),
    (
        9003,
        9002,
        9001,
        1,
        "2024-06-03T15:00:00Z",
        "2024-06-03T16:00:00Z",
        "booked",
    ),
    (
        9004,
        9003,
        9001,
        1,
        "2024-06-03T15:30:00Z",
        "2024-06-03T16:30:00Z",
        "booked",
    ),
)
PLANTED_VISITS = (
    (9001, 9004, 9001, None, 1, "2024-07-01T15:00:00Z", 250000),
    (9002, 9005, 9001, None, 9001, "2024-01-05T15:00:00Z", 12000),
    (9003, 9005, 9001, None, 9001, "2024-08-01T15:00:00Z", 12000),
    (9004, 9005, 9001, None, 9002, "2024-04-02T15:00:00Z", 12000),
)
PLANTED_INVOICES = (
    (9001, 9001, None, "2024-07-02", "paid", 10000),
    (9002, 9001, 9002, "2024-01-05", "open", 2500),
    (9003, 9001, 9003, "2024-08-01", "open", 1000),
    (9004, 9001, 9004, "2024-04-02", "open", 500),
)
PLANTED_INVOICE_LINES = (
    (9001, 9001, None, "synthetic line", 3000),
    (9002, 9001, None, "synthetic line", 4000),
    (9003, 9002, 9001, "synthetic line", 2500),
    (9004, 9003, None, "synthetic line", 1000),
    (9005, 9004, 9003, "synthetic line", 500),
)
PLANTED_PRODUCTS = (
    (1, "SYN-SUPPLY", "supply"),
    (9001, "SYN-VACCINE", "vaccine"),
    (9002, "SYN-PRICE-GAP", "supply"),
    (9003, "SYN-OVERORDER", "supply"),
)
PLANTED_USAGE = (
    (1, 1, 1, 1),
    (9001, 9001, 9001, 1),
    (9002, 9002, 9003, 1),
)
PLANTED_SUPPLIERS = (
    (1, "Synthetic Supplier 1", 2, 1),
    (9001, "Synthetic Supplier 9001", 5, 10),
)
PLANTED_SUPPLIER_PRICES = (
    (1, 1, 1, "2024-01-01", 400),
    (9001, 9001, 9002, "2024-01-01", 500),
    (9002, 9001, 9002, "2024-06-01", 800),
    (9003, 9001, 9001, "2024-01-01", 1500),
    (9004, 9001, 9003, "2024-01-01", 200),
)
PLANTED_CLIENT_PRICES = (
    (1, 1, "2024-01-01", 900),
    (9001, 9002, "2024-01-01", 1200),
    (9002, 9001, "2024-01-01", 2500),
    (9003, 9003, "2024-01-01", 500),
)
PLANTED_LOTS = (
    (9001, 9001, 9001, "2024-01-02", "2024-01-07", 2, 1, 1500),
    (9002, 9001, 9001, "2024-07-01", "2025-07-01", 5, 4, 1500),
    (9003, 9002, 9001, "2024-03-01", "2025-03-01", 1, 1, 500),
    (9004, 9002, 9001, "2024-07-15", "2025-07-15", 1, 1, 800),
    (9005, 9003, 9001, "2024-04-01", "2025-04-01", 24, 23, 200),
    (9006, 1, 1, "2024-06-30", "2025-06-30", 1, 0, 400),
)
PLANTED_DELIVERIES = (
    (9001, 9001, 9001, 9001, "2024-01-02", 2, 1500),
    (9002, 9001, 9001, 9002, "2024-07-01", 5, 1500),
    (9003, 9001, 9002, 9003, "2024-03-01", 1, 500),
    (9004, 9001, 9002, 9004, "2024-07-15", 1, 800),
    (9005, 9001, 9003, 9005, "2024-04-01", 24, 200),
    (9006, 1, 1, 9006, "2024-06-30", 1, 400),
)

TABLE_COLUMNS = {
    "provenance": (
        "provenance_id",
        "data_class",
        "label",
        "notice",
        "generator",
        "seed",
        "clock_start",
        "clock_end",
        "locale",
    ),
    "clinician": ("clinician_id", "full_name", "role"),
    "owner": ("owner_id", "full_name", "email", "phone", "city"),
    "patient": (
        "patient_id",
        "owner_id",
        "patient_name",
        "species",
        "breed",
        "sex",
        "date_of_birth",
        "microchip",
        "status",
        "deceased_on",
    ),
    "appointment_type": ("appointment_type_id", "type_name"),
    "appointment": (
        "appointment_id",
        "patient_id",
        "clinician_id",
        "appointment_type_id",
        "starts_at",
        "ends_at",
        "status",
    ),
    "visit": (
        "visit_id",
        "patient_id",
        "clinician_id",
        "appointment_id",
        "appointment_type_id",
        "visited_at",
        "weight_g",
    ),
    "invoice": (
        "invoice_id",
        "owner_id",
        "visit_id",
        "issued_on",
        "status",
        "total_cents",
    ),
    "invoice_line": (
        "line_id",
        "invoice_id",
        "product_id",
        "description",
        "amount_cents",
    ),
    "product": ("product_id", "product_name", "kind"),
    "usage": ("usage_id", "appointment_type_id", "product_id", "qty"),
    "supplier": (
        "supplier_id",
        "supplier_name",
        "lead_time_days",
        "min_order_qty",
    ),
    "supplier_price": (
        "supplier_price_id",
        "supplier_id",
        "product_id",
        "effective_on",
        "unit_cents",
    ),
    "client_price": (
        "client_price_id",
        "product_id",
        "effective_on",
        "unit_cents",
    ),
    "inventory_lot": (
        "lot_id",
        "product_id",
        "supplier_id",
        "received_on",
        "expires_on",
        "qty_received",
        "qty_on_hand",
        "unit_cents",
    ),
    "delivery": (
        "delivery_id",
        "supplier_id",
        "product_id",
        "lot_id",
        "delivered_on",
        "qty",
        "unit_cents",
    ),
    "stock_use": (
        "stock_use_id",
        "visit_id",
        "lot_id",
        "product_id",
        "qty",
        "used_on",
    ),
}

SCHEMA_SQL = """\
CREATE TABLE provenance (
    provenance_id INTEGER PRIMARY KEY CHECK (provenance_id = 1),
    data_class TEXT NOT NULL CHECK (data_class = 'synthetic'),
    label TEXT NOT NULL CHECK (label = 'synthetic'),
    notice TEXT NOT NULL,
    generator TEXT NOT NULL,
    seed INTEGER NOT NULL CHECK (seed >= 0),
    clock_start TEXT NOT NULL,
    clock_end TEXT NOT NULL,
    locale TEXT NOT NULL
);
CREATE TABLE clinician (
    clinician_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL
);
CREATE TABLE owner (
    owner_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    city TEXT NOT NULL
);
CREATE TABLE patient (
    patient_id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES owner (owner_id),
    patient_name TEXT NOT NULL,
    species TEXT NOT NULL CHECK (species IN ('dog', 'cat', 'rabbit')),
    breed TEXT NOT NULL,
    sex TEXT NOT NULL CHECK (sex IN ('F', 'M')),
    date_of_birth TEXT NOT NULL,
    microchip TEXT,
    status TEXT NOT NULL CHECK (status IN ('alive', 'deceased')),
    deceased_on TEXT,
    CHECK (
        (status = 'alive' AND deceased_on IS NULL)
        OR (status = 'deceased' AND deceased_on IS NOT NULL)
    )
);
CREATE TABLE appointment_type (
    appointment_type_id INTEGER PRIMARY KEY,
    type_name TEXT NOT NULL UNIQUE
        CHECK (type_name IN ('checkup', 'vaccine', 'overorder'))
);
CREATE TABLE appointment (
    appointment_id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patient (patient_id),
    clinician_id INTEGER NOT NULL REFERENCES clinician (clinician_id),
    appointment_type_id INTEGER NOT NULL
        REFERENCES appointment_type (appointment_type_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE visit (
    visit_id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patient (patient_id),
    clinician_id INTEGER NOT NULL REFERENCES clinician (clinician_id),
    appointment_id INTEGER REFERENCES appointment (appointment_id),
    appointment_type_id INTEGER NOT NULL
        REFERENCES appointment_type (appointment_type_id),
    visited_at TEXT NOT NULL,
    weight_g INTEGER NOT NULL CHECK (weight_g > 0)
);
CREATE TABLE invoice (
    invoice_id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES owner (owner_id),
    visit_id INTEGER REFERENCES visit (visit_id),
    issued_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'paid')),
    total_cents INTEGER NOT NULL
);
CREATE TABLE invoice_line (
    line_id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoice (invoice_id),
    product_id INTEGER REFERENCES product (product_id),
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL
);
CREATE TABLE product (
    product_id INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('vaccine', 'supply'))
);
CREATE TABLE usage (
    usage_id INTEGER PRIMARY KEY,
    appointment_type_id INTEGER NOT NULL
        REFERENCES appointment_type (appointment_type_id),
    product_id INTEGER NOT NULL REFERENCES product (product_id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    UNIQUE (appointment_type_id, product_id)
);
CREATE TABLE supplier (
    supplier_id INTEGER PRIMARY KEY,
    supplier_name TEXT NOT NULL,
    lead_time_days INTEGER NOT NULL CHECK (lead_time_days >= 0),
    min_order_qty INTEGER NOT NULL CHECK (min_order_qty >= 1)
);
CREATE TABLE supplier_price (
    supplier_price_id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES supplier (supplier_id),
    product_id INTEGER NOT NULL REFERENCES product (product_id),
    effective_on TEXT NOT NULL,
    unit_cents INTEGER NOT NULL CHECK (unit_cents >= 0)
);
CREATE TABLE client_price (
    client_price_id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product (product_id),
    effective_on TEXT NOT NULL,
    unit_cents INTEGER NOT NULL CHECK (unit_cents >= 0)
);
CREATE TABLE inventory_lot (
    lot_id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product (product_id),
    supplier_id INTEGER NOT NULL REFERENCES supplier (supplier_id),
    received_on TEXT NOT NULL,
    expires_on TEXT NOT NULL,
    qty_received INTEGER NOT NULL CHECK (qty_received > 0),
    qty_on_hand INTEGER NOT NULL CHECK (qty_on_hand >= 0),
    unit_cents INTEGER NOT NULL CHECK (unit_cents >= 0)
);
CREATE TABLE delivery (
    delivery_id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES supplier (supplier_id),
    product_id INTEGER NOT NULL REFERENCES product (product_id),
    lot_id INTEGER NOT NULL REFERENCES inventory_lot (lot_id),
    delivered_on TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    unit_cents INTEGER NOT NULL CHECK (unit_cents >= 0)
);
CREATE TABLE stock_use (
    stock_use_id INTEGER PRIMARY KEY,
    visit_id INTEGER NOT NULL REFERENCES visit (visit_id),
    lot_id INTEGER NOT NULL REFERENCES inventory_lot (lot_id),
    product_id INTEGER NOT NULL REFERENCES product (product_id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    used_on TEXT NOT NULL
);
CREATE INDEX idx_patient_owner ON patient (owner_id);
CREATE INDEX idx_appointment_patient ON appointment (patient_id);
CREATE INDEX idx_appointment_clinician ON appointment (clinician_id);
CREATE INDEX idx_visit_patient ON visit (patient_id);
CREATE INDEX idx_invoice_owner ON invoice (owner_id);
CREATE INDEX idx_invoice_line_invoice ON invoice_line (invoice_id);
CREATE INDEX idx_supplier_price_product
    ON supplier_price (product_id, effective_on);
CREATE INDEX idx_delivery_product ON delivery (product_id);
CREATE INDEX idx_stock_use_visit ON stock_use (visit_id);
CREATE INDEX idx_stock_use_product ON stock_use (product_id);
CREATE INDEX idx_inventory_lot_product ON inventory_lot (product_id);
CREATE INDEX idx_usage_appointment_type ON usage (appointment_type_id);
"""


def quote_ident(name: str) -> str:
    """Double-quote a SQLite identifier, doubling embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class PlantedCase:
    id: str
    sql: str
    expected_rows: tuple[tuple[int | str, ...], ...]


_DECEASED_APPOINTMENT = (
    "SELECT p.patient_id, p.deceased_on, a.appointment_id, a.starts_at\n"
    "FROM patient p\n"
    "JOIN appointment a ON a.patient_id = p.patient_id\n"
    "WHERE p.status = 'deceased' AND a.starts_at > p.deceased_on;"
)
_INVOICE_TOTAL_MISMATCH = (
    "SELECT i.invoice_id, i.status, i.total_cents,"
    " SUM(l.amount_cents) AS line_cents\n"
    "FROM invoice i\n"
    "JOIN invoice_line l ON l.invoice_id = i.invoice_id\n"
    "GROUP BY i.invoice_id, i.status, i.total_cents\n"
    "HAVING i.total_cents <> SUM(l.amount_cents);"
)
_APPOINTMENT_ENDS_BEFORE_START = (
    "SELECT appointment_id, starts_at, ends_at\n"
    "FROM appointment\n"
    "WHERE ends_at < starts_at;"
)
_SHARED_MICROCHIP = (
    "SELECT microchip, COUNT(*) AS n\n"
    "FROM patient\n"
    "WHERE microchip IS NOT NULL\n"
    "GROUP BY microchip\n"
    "HAVING COUNT(*) > 1;"
)
_CLINICIAN_OVERLAP = (
    "SELECT a.appointment_id, b.appointment_id, a.clinician_id\n"
    "FROM appointment a\n"
    "JOIN appointment b\n"
    "  ON a.clinician_id = b.clinician_id\n"
    " AND a.appointment_id < b.appointment_id\n"
    " AND a.starts_at < b.ends_at\n"
    " AND b.starts_at < a.ends_at;"
)
_IMPOSSIBLE_WEIGHT = (
    "SELECT v.visit_id, p.patient_id, p.species, v.weight_g\n"
    "FROM visit v\n"
    "JOIN patient p ON p.patient_id = v.patient_id\n"
    "WHERE p.species = 'cat' AND v.weight_g > 20000;"
)
_VACCINE_NOT_BILLED = (
    "SELECT v.visit_id, v.patient_id, su.product_id, pr.product_name\n"
    "FROM visit v\n"
    "JOIN stock_use su ON su.visit_id = v.visit_id\n"
    "JOIN product pr ON pr.product_id = su.product_id\n"
    "LEFT JOIN invoice i ON i.visit_id = v.visit_id\n"
    "LEFT JOIN invoice_line l\n"
    "  ON l.invoice_id = i.invoice_id AND l.product_id = su.product_id\n"
    "WHERE pr.kind = 'vaccine' AND l.line_id IS NULL;"
)
_SUPPLIER_PRICE_UP_CLIENT_FLAT = (
    "SELECT sp_new.product_id,\n"
    "       sp_old.unit_cents AS old_supplier_cents,\n"
    "       sp_new.unit_cents AS new_supplier_cents,\n"
    "       cp.unit_cents AS client_cents\n"
    "FROM supplier_price sp_new\n"
    "JOIN supplier_price sp_old\n"
    "  ON sp_old.product_id = sp_new.product_id\n"
    " AND sp_old.supplier_id = sp_new.supplier_id\n"
    " AND sp_old.effective_on < sp_new.effective_on\n"
    " AND sp_old.unit_cents < sp_new.unit_cents\n"
    "JOIN client_price cp ON cp.product_id = sp_new.product_id\n"
    "WHERE NOT EXISTS (\n"
    "    SELECT 1 FROM client_price later\n"
    "    WHERE later.product_id = cp.product_id\n"
    "      AND later.effective_on > cp.effective_on\n"
    ")\n"
    "  AND NOT EXISTS (\n"
    "    SELECT 1 FROM supplier_price newer\n"
    "    WHERE newer.product_id = sp_new.product_id\n"
    "      AND newer.supplier_id = sp_new.supplier_id\n"
    "      AND newer.effective_on > sp_new.effective_on\n"
    ");"
)
_OVER_ORDER = (
    "SELECT p.product_id, p.product_name,\n"
    "       (SELECT COALESCE(SUM(d.qty), 0) FROM delivery d\n"
    "         WHERE d.product_id = p.product_id) AS delivered_qty,\n"
    "       (SELECT COALESCE(SUM(u.qty), 0) FROM stock_use u\n"
    "         WHERE u.product_id = p.product_id) AS used_qty\n"
    "FROM product p\n"
    "WHERE (SELECT COALESCE(SUM(u.qty), 0) FROM stock_use u\n"
    "         WHERE u.product_id = p.product_id) > 0\n"
    "  AND (SELECT COALESCE(SUM(d.qty), 0) FROM delivery d\n"
    "         WHERE d.product_id = p.product_id)\n"
    "      > 4 * (SELECT COALESCE(SUM(u.qty), 0) FROM stock_use u\n"
    "              WHERE u.product_id = p.product_id);"
)

PLANTED_CASES: tuple[PlantedCase, ...] = (
    PlantedCase(
        id="deceased_appointment",
        sql=_DECEASED_APPOINTMENT,
        expected_rows=((9001, "2024-03-01", 9001, "2024-04-01T15:00:00Z"),),
    ),
    PlantedCase(
        id="invoice_total_mismatch",
        sql=_INVOICE_TOTAL_MISMATCH,
        expected_rows=((9001, "paid", 10000, 7000),),
    ),
    PlantedCase(
        id="appointment_ends_before_start",
        sql=_APPOINTMENT_ENDS_BEFORE_START,
        expected_rows=((9002, "2024-05-02T15:00:00Z", "2024-05-02T14:00:00Z"),),
    ),
    PlantedCase(
        id="shared_microchip",
        sql=_SHARED_MICROCHIP,
        expected_rows=(("SYN-CHIP-9000", 2),),
    ),
    PlantedCase(
        id="clinician_overlap",
        sql=_CLINICIAN_OVERLAP,
        expected_rows=((9003, 9004, 9001),),
    ),
    PlantedCase(
        id="impossible_weight",
        sql=_IMPOSSIBLE_WEIGHT,
        expected_rows=((9001, 9004, "cat", 250000),),
    ),
    PlantedCase(
        id="vaccine_not_billed",
        sql=_VACCINE_NOT_BILLED,
        expected_rows=((9003, 9005, 9001, "SYN-VACCINE"),),
    ),
    PlantedCase(
        id="supplier_price_up_client_flat",
        sql=_SUPPLIER_PRICE_UP_CLIENT_FLAT,
        expected_rows=((9002, 500, 800, 1200),),
    ),
    PlantedCase(
        id="over_order",
        sql=_OVER_ORDER,
        expected_rows=((9003, "SYN-OVERORDER", 24, 1),),
    ),
)

CLINIC_SAMPLE_QUERIES: tuple[str, ...] = (
    "Which vaccines were given at a visit and not billed on that visit's invoice?",
    (
        "Which products have a supplier price that went up "
        "while the client price stayed the same?"
    ),
    "Which products were delivered in more than four times the quantity that was used?",
)
