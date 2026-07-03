"""
نظام حجز صالونات الحلاقة — Multi-Tenant على PostgreSQL
=========================================================
* قاعدة بيانات PostgreSQL واحدة فقط (لا يوجد SQLite إطلاقاً).
* بيانات المؤسسات (tenants) والاشتراكات في الـ schema الرئيسي "public".
* كل صالون له Schema منفصلة داخل نفس قاعدة البيانات باسم tenant_<slug>
  تحتوي على جداوله: users / system_settings / services / customers / bookings.
* عند فتح أي صفحة خاصة بصالون، يتم تنفيذ SET search_path على الاتصال
  للتبديل تلقائياً إلى الـ schema الخاصة به.
* بيانات الاتصال تُقرأ حصراً من متغير البيئة DATABASE_URL (كما يوفرها Railway
  تلقائياً عند إضافة خدمة PostgreSQL للمشروع).
"""

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, g
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker
from datetime import datetime, date, timedelta
from functools import wraps
import os, re, secrets

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'salon_multitenant_secret_2024_change_in_prod')

# ══════════════════════════════════════════════════════════════
#  حساب مطور النظام (مخفي تماماً - غير موجود في أي جدول بيانات)
# ══════════════════════════════════════════════════════════════
DEVELOPER_USERNAME = os.environ.get('DEVELOPER_USERNAME', 'administrator')
DEVELOPER_PASSWORD = os.environ.get('DEVELOPER_PASSWORD', '3000330210')

@app.template_filter('format_date')
def format_date_filter(value, fmt='%Y/%m/%d'):
    if not value:
        return ''
    if hasattr(value, 'strftime'):
        return value.strftime(fmt)
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(str(value)[:19], pattern).strftime(fmt)
        except ValueError:
            continue
    return str(value)[:10]

# ══════════════════════════════════════════════════════════════
#  الاتصال بقاعدة بيانات PostgreSQL (المصدر الوحيد: DATABASE_URL)
# ══════════════════════════════════════════════════════════════

def _normalize_db_url(url: str) -> str:
    """Railway / Heroku تُصدر أحياناً بادئة postgres:// القديمة وهي غير
    مدعومة في SQLAlchemy 2.x، فنحوّلها إلى postgresql://"""
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return url

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    # قيمة افتراضية للتطوير المحلي فقط — يجب ضبط DATABASE_URL في أي بيئة حقيقية
    DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/salon_saas'
DATABASE_URL = _normalize_db_url(DATABASE_URL)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 1800,
}
main_db = SQLAlchemy(app)

# محرك واحد مشترك لجميع الصالونات (raw SQL) — يُستخدم مع search_path الديناميكي
tenant_engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=1800,
)
TenantSessionFactory = scoped_session(sessionmaker(bind=tenant_engine))


class Tenant(main_db.Model):
    __tablename__ = 'tenants'
    __table_args__ = {'schema': 'public'}
    id            = main_db.Column(main_db.Integer, primary_key=True)
    slug          = main_db.Column(main_db.String(60), unique=True, nullable=False)
    org_name      = main_db.Column(main_db.String(200), nullable=False)
    owner_name    = main_db.Column(main_db.String(100), nullable=False)
    owner_email   = main_db.Column(main_db.String(150), unique=True, nullable=False)
    owner_phone   = main_db.Column(main_db.String(20), nullable=True)
    owner_password= main_db.Column(main_db.String(200), nullable=False)
    is_active     = main_db.Column(main_db.Boolean, default=True)
    subscription_until = main_db.Column(main_db.DateTime, nullable=True)
    created_at    = main_db.Column(main_db.DateTime, default=datetime.utcnow)

    def is_subscribed(self):
        return bool(self.subscription_until and self.subscription_until > datetime.utcnow())

    def schema_name(self):
        return tenant_schema_name(self.slug)


class SubscriptionCode(main_db.Model):
    __tablename__ = 'subscription_codes'
    __table_args__ = {'schema': 'public'}
    id         = main_db.Column(main_db.Integer, primary_key=True)
    code       = main_db.Column(main_db.String(40), unique=True, nullable=False)
    slug       = main_db.Column(main_db.String(60), nullable=False)
    months     = main_db.Column(main_db.Integer, nullable=False)
    is_used    = main_db.Column(main_db.Boolean, default=False)
    used_at    = main_db.Column(main_db.DateTime, nullable=True)
    created_at = main_db.Column(main_db.DateTime, default=datetime.utcnow)

# ══════════════════════════════════════════════════════════════
#  حدود الباقة المجانية + باقات الاشتراك
# ══════════════════════════════════════════════════════════════
FREE_PLAN_MAX_USERS     = 2
FREE_PLAN_MAX_CUSTOMERS = 30
VIP_VISIT_THRESHOLD     = 5     # عدد الزيارات التي تجعل العميل "مميز" تلقائياً
SUBSCRIPTION_WHATSAPP   = '01103763082'
SUBSCRIPTION_PLANS = [
    {'months': 6,  'price': 3000, 'label': '6 أشهر'},
    {'months': 12, 'price': 5500, 'label': 'سنة كاملة'},
]

# ══════════════════════════════════════════════════════════════
#  Schema كل مؤسسة داخل PostgreSQL (بديل ملف SQLite المنفصل)
# ══════════════════════════════════════════════════════════════

def tenant_schema_name(slug: str) -> str:
    """اسم الـ schema الخاص بالمؤسسة. slug مُقيّد مسبقاً بحروف/أرقام/شرطة سفلية."""
    safe = re.sub(r'[^a-z0-9_]', '', slug.lower())
    return f'tenant_{safe}'

TENANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    username    VARCHAR(150) UNIQUE NOT NULL,
    password    VARCHAR(200) NOT NULL,
    name        VARCHAR(150) NOT NULL,
    role        VARCHAR(30)  DEFAULT 'barber',
    permissions TEXT DEFAULT '[]',
    is_active   INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS system_settings (
    id              SERIAL PRIMARY KEY,
    system_name     VARCHAR(200) DEFAULT 'نظام حجز الصالون',
    system_subtitle VARCHAR(200) DEFAULT 'Salon Booking System',
    salon_name      VARCHAR(200) DEFAULT '',
    salon_address   VARCHAR(255) DEFAULT '',
    admin_phone     VARCHAR(30)  DEFAULT '',
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS services (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(150) NOT NULL,
    price      NUMERIC(10,2) DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS customers (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(150) NOT NULL,
    phone       VARCHAR(30) UNIQUE NOT NULL,
    visit_count INTEGER DEFAULT 0,
    is_vip      INTEGER DEFAULT 0,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS bookings (
    id             SERIAL PRIMARY KEY,
    customer_id    INTEGER NOT NULL,
    customer_name  VARCHAR(150) NOT NULL,
    customer_phone VARCHAR(30) NOT NULL,
    service_id     INTEGER,
    service_name   VARCHAR(150) DEFAULT '',
    queue_number   INTEGER NOT NULL,
    booking_date   TEXT NOT NULL,
    status         VARCHAR(20) DEFAULT 'waiting',
    notes          TEXT DEFAULT '',
    created_at     TEXT,
    completed_at   TEXT
);
"""

# كاش في الذاكرة لعدم إعادة تنفيذ "CREATE SCHEMA / CREATE TABLE" مع كل طلب
_migrated_schemas = set()

def ensure_tenant_schema(slug: str) -> str:
    """ينشئ (أو يهاجر / Migration تلقائية) الـ schema وجداولها إن لم تكن موجودة.
    آمن للاستدعاء المتكرر (كل الجمل IF NOT EXISTS)."""
    schema = tenant_schema_name(slug)
    with tenant_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        conn.execute(text(f'SET search_path TO "{schema}"'))
        for stmt in TENANT_SCHEMA.strip().split(';'):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
    _migrated_schemas.add(slug)
    return schema

def create_tenant_schema(slug, org_name, owner_name, owner_email, owner_password):
    """إنشاء Schema صالون جديد بالكامل (بديل create_tenant_db القديمة لـ SQLite)."""
    schema = ensure_tenant_schema(slug)
    with tenant_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}"'))
        conn.execute(text(
            "INSERT INTO users (username,password,name,role,permissions,is_active) "
            "VALUES (:u,:p,:n,'admin','[]',1) ON CONFLICT (username) DO NOTHING"
        ), {'u': owner_email, 'p': owner_password, 'n': owner_name})
        conn.execute(text(
            "INSERT INTO system_settings (system_name,system_subtitle,salon_name,updated_at) "
            "VALUES (:n,'Salon Booking System',:o,:d)"
        ), {'n': org_name, 'o': org_name, 'd': datetime.utcnow().isoformat()})
        default_services = [('قصة شعر', 50), ('حلاقة ذقن', 30), ('قصة + ذقن', 70)]
        for name, price in default_services:
            conn.execute(text(
                "INSERT INTO services (name, price, created_at) VALUES (:n,:p,:d)"
            ), {'n': name, 'p': price, 'd': datetime.utcnow().isoformat()})

def drop_tenant_schema(slug: str):
    schema = tenant_schema_name(slug)
    with tenant_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    _migrated_schemas.discard(slug)

# ══════════════════════════════════════════════════════════════
#  جلسة قاعدة البيانات الخاصة بالصالون الحالي (Dynamic search_path)
# ══════════════════════════════════════════════════════════════

def get_tenant_session(slug):
    """يعيد Session مرتبطة بنفس اتصال PostgreSQL المشترك، بعد تحويل
    search_path تلقائياً إلى Schema المؤسسة المطلوبة (بديل فتح ملف
    SQLite منفصل لكل مؤسسة)."""
    if slug not in _migrated_schemas:
        ensure_tenant_schema(slug)
    sess = TenantSessionFactory()
    schema = tenant_schema_name(slug)
    sess.execute(text(f'SET search_path TO "{schema}", public'))
    return sess

# ══════════════════════════════════════════════════════════════
#  Helpers للاستعلامات على قاعدة بيانات الصالون
# ══════════════════════════════════════════════════════════════

def tdb():
    return g.tenant_session

def t_fetchall(sql, params=None):
    result = tdb().execute(text(sql), params or {})
    rows   = result.fetchall()
    keys   = result.keys()
    return [dict(zip(keys, row)) for row in rows]

def t_fetchone(sql, params=None):
    result = tdb().execute(text(sql), params or {})
    row    = result.fetchone()
    return dict(zip(result.keys(), row)) if row else None

def t_execute(sql, params=None):
    tdb().execute(text(sql), params or {})
    tdb().commit()

def t_insert_returning_id(sql, params=None):
    """ينفذ استعلام INSERT ويعيد الـ id الجديد باستخدام RETURNING
    (بديل last_insert_rowid() الخاص بـ SQLite)."""
    clean = sql.strip().rstrip(';')
    if 'returning' not in clean.lower():
        clean += ' RETURNING id'
    new_id = tdb().execute(text(clean), params or {}).scalar()
    tdb().commit()
    return new_id

# ══════════════════════════════════════════════════════════════
#  ثوابت مشتركة
# ══════════════════════════════════════════════════════════════

PERMISSIONS = {
    'view_bookings':    'عرض الحجوزات',
    'manage_bookings':  'إدارة الحجوزات (إنهاء / إلغاء)',
    'manage_customers': 'إدارة العملاء',
    'reports':          'عرض الإحصائيات',
}

# ══════════════════════════════════════════════════════════════
#  Decorators
# ══════════════════════════════════════════════════════════════

def load_tenant(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        slug   = kwargs.get('slug', '')
        tenant = Tenant.query.filter_by(slug=slug, is_active=True).first()
        if not tenant:
            return render_template('404.html'), 404
        g.tenant      = tenant
        g.tenant_slug = slug
        g.tenant_session = get_tenant_session(slug)
        s = t_fetchone("SELECT * FROM system_settings LIMIT 1")
        g.sys_name     = s['system_name']     if s else 'نظام حجز الصالون'
        g.sys_subtitle = s['system_subtitle'] if s else 'Salon Booking System'
        g.is_subscribed = tenant.is_subscribed()
        g.subscription_until = tenant.subscription_until
        if not g.is_subscribed:
            users_count     = t_fetchone("SELECT COUNT(*) AS c FROM users")['c']
            customers_count = t_fetchone("SELECT COUNT(*) AS c FROM customers")['c']
            g.limit_reached = (users_count >= FREE_PLAN_MAX_USERS) or (customers_count >= FREE_PLAN_MAX_CUSTOMERS)
        else:
            g.limit_reached = False
        return f(*args, **kwargs)
    return decorated

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or session.get('tenant_slug') != kwargs.get('slug',''):
            return redirect(url_for('tenant_login', slug=kwargs.get('slug','')))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('user_role') != 'admin':
            flash('هذه الصفحة للمديرين فقط', 'warning')
            return redirect(url_for('dashboard', slug=kwargs.get('slug','')))
        return f(*args, **kwargs)
    return decorated

def developer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_developer'):
            return render_template('404.html'), 404
        return f(*args, **kwargs)
    return decorated

@app.teardown_appcontext
def close_tenant_session(exception=None):
    sess = g.pop('tenant_session', None)
    if sess is not None:
        if exception:
            sess.rollback()
        else:
            try:
                sess.commit()
            except Exception:
                sess.rollback()
        sess.close()

# ══════════════════════════════════════════════════════════════
#  الصفحات العامة (قبل الدخول)
# ══════════════════════════════════════════════════════════════

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/register', methods=['GET','POST'])
def register():
    error = None
    if request.method == 'POST':
        org_name  = request.form.get('org_name','').strip()
        slug_raw  = request.form.get('slug','').strip().lower()
        slug      = re.sub(r'[^a-z0-9_]', '', slug_raw)
        owner     = request.form.get('owner_name','').strip()
        owner_phone = re.sub(r'[^0-9]', '', request.form.get('owner_phone','').strip())
        email_username = re.sub(r'[^a-z0-9._-]', '', request.form.get('email_username','').strip().lower())
        email     = email_username + '@sysmakers.com' if email_username else ''
        password  = request.form.get('password','').strip()
        password2 = request.form.get('password2','').strip()

        MAX_TENANTS_PER_PHONE = 2

        if not all([org_name, slug, owner, owner_phone, email, password]):
            error = 'يرجى تعبئة جميع الحقول'
        elif len(owner_phone) < 8:
            error = 'رقم الهاتف غير صحيح'
        elif len(slug) < 3:
            error = 'رمز الصالون يجب أن يكون 3 أحرف على الأقل'
        elif password != password2:
            error = 'كلمتا المرور غير متطابقتين'
        elif len(password) < 6:
            error = 'كلمة المرور يجب أن تكون 6 أحرف على الأقل'
        elif slug == 'administrator':
            error = 'رمز الصالون غير متاح، اختر رمزاً آخر'
        elif Tenant.query.filter_by(owner_phone=owner_phone).count() >= MAX_TENANTS_PER_PHONE:
            error = 'تعذر إتمام عملية التسجيل، يرجى المحاولة لاحقاً أو التواصل مع الدعم الفني'
        elif Tenant.query.filter_by(slug=slug).first():
            error = 'رمز الصالون مستخدم، اختر رمزاً آخر'
        elif Tenant.query.filter_by(owner_email=email).first():
            error = 'البريد الإلكتروني مسجل مسبقاً'
        else:
            tenant = Tenant(slug=slug, org_name=org_name, owner_name=owner, owner_phone=owner_phone,
                            owner_email=email, owner_password=password)
            main_db.session.add(tenant)
            main_db.session.commit()
            create_tenant_schema(slug, org_name, owner, email, password)
            flash(f'تم إنشاء صالونك بنجاح! | بريدك: {email} | رابط الدخول: /org/{slug}/login', 'success')
            return redirect(url_for('tenant_login', slug=slug))
    return render_template('register.html', error=error)

# ══════════════════════════════════════════════════════════════
#  روابط الصالون /org/<slug>/...
# ══════════════════════════════════════════════════════════════

@app.route('/org/<slug>/')
@app.route('/org/<slug>/login', methods=['GET','POST'])
@load_tenant
def tenant_login(slug):
    if 'user_id' in session and session.get('tenant_slug') == slug:
        return redirect(url_for('dashboard', slug=slug))
    error = None
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','').strip()
        user = t_fetchone(
            "SELECT * FROM users WHERE username=:u AND password=:p AND is_active=1",
            {'u': username, 'p': password}
        )
        if user:
            session.clear()
            session['user_id']    = user['id']
            session['user_name']  = user['name']
            session['user_role']  = user['role']
            session['tenant_slug']= slug
            return redirect(url_for('dashboard', slug=slug))
        error = 'اسم المستخدم أو كلمة المرور غير صحيحة'
    return render_template('login.html', error=error, slug=slug, org_name=g.tenant.org_name)

@app.route('/org/<slug>/logout')
def tenant_logout(slug):
    session.clear()
    return redirect(url_for('tenant_login', slug=slug))

# ─── Public Customer Booking (بدون تسجيل دخول) ─────────────────

@app.route('/org/<slug>/book', methods=['GET','POST'])
@load_tenant
def book(slug):
    services = t_fetchall("SELECT * FROM services ORDER BY id")
    today    = date.today().isoformat()
    waiting_count = t_fetchone(
        "SELECT COUNT(*) c FROM bookings WHERE booking_date=:d AND status='waiting'", {'d': today}
    )['c']
    error = None
    if request.method == 'POST':
        name  = request.form.get('name','').strip()
        phone = re.sub(r'[^0-9]', '', request.form.get('phone','').strip())
        service_id = request.form.get('service_id') or None
        if not name or len(phone) < 8:
            error = 'يرجى إدخال الاسم ورقم هاتف صحيح'
        else:
            customer = t_fetchone("SELECT * FROM customers WHERE phone=:p", {'p': phone})
            if not customer:
                if not g.is_subscribed and t_fetchone("SELECT COUNT(*) c FROM customers")['c'] >= FREE_PLAN_MAX_CUSTOMERS:
                    error = 'الصالون وصل للحد الأقصى من العملاء حالياً، يرجى التواصل مع الصالون مباشرة'
                    return render_template('book.html', slug=slug, org_name=g.tenant.org_name,
                                           services=services, waiting_count=waiting_count, error=error)
                t_execute(
                    "INSERT INTO customers (name, phone, visit_count, is_vip, created_at) VALUES (:n,:p,0,0,:d)",
                    {'n': name, 'p': phone, 'd': datetime.utcnow().isoformat()}
                )
                customer = t_fetchone("SELECT * FROM customers WHERE phone=:p", {'p': phone})
            else:
                t_execute("UPDATE customers SET name=:n WHERE id=:id", {'n': name, 'id': customer['id']})

            service_name = ''
            if service_id:
                sv = t_fetchone("SELECT * FROM services WHERE id=:id", {'id': service_id})
                service_name = sv['name'] if sv else ''

            last_q = t_fetchone(
                "SELECT MAX(queue_number) m FROM bookings WHERE booking_date=:d", {'d': today}
            )['m'] or 0
            queue_number = last_q + 1

            booking_id = t_insert_returning_id(
                "INSERT INTO bookings (customer_id, customer_name, customer_phone, service_id, service_name, "
                "queue_number, booking_date, status, notes, created_at) "
                "VALUES (:cid,:cn,:cp,:sid,:sn,:q,:bd,'waiting','',:ca)",
                {'cid': customer['id'], 'cn': name, 'cp': phone, 'sid': service_id, 'sn': service_name,
                 'q': queue_number, 'bd': today, 'ca': datetime.utcnow().isoformat()}
            )
            return redirect(url_for('booking_status', slug=slug, booking_id=booking_id))
    return render_template('book.html', slug=slug, org_name=g.tenant.org_name,
                           services=services, waiting_count=waiting_count, error=error)

@app.route('/org/<slug>/book/status/<int:booking_id>')
@load_tenant
def booking_status(slug, booking_id):
    booking = t_fetchone("SELECT * FROM bookings WHERE id=:id", {'id': booking_id})
    if not booking:
        return render_template('404.html'), 404
    return render_template('booking_status.html', slug=slug, org_name=g.tenant.org_name, booking=booking)

@app.route('/org/<slug>/api/booking_status/<int:booking_id>')
@load_tenant
def api_booking_status(slug, booking_id):
    booking = t_fetchone("SELECT * FROM bookings WHERE id=:id", {'id': booking_id})
    if not booking:
        return jsonify({'error':'not found'}), 404
    ahead = 0
    if booking['status'] == 'waiting':
        ahead = t_fetchone(
            "SELECT COUNT(*) c FROM bookings WHERE booking_date=:d AND status='waiting' AND queue_number < :q",
            {'d': booking['booking_date'], 'q': booking['queue_number']}
        )['c']
    return jsonify({
        'status': booking['status'],
        'queue_number': booking['queue_number'],
        'ahead_of_you': ahead,
        'notes': booking['notes']
    })

# ─── Dashboard ────────────────────────────────────────────────

@app.route('/org/<slug>/dashboard')
@load_tenant
@login_required
def dashboard(slug):
    today   = date.today().isoformat()
    total_today   = t_fetchone("SELECT COUNT(*) c FROM bookings WHERE booking_date=:d", {'d': today})['c']
    waiting_today = t_fetchone("SELECT COUNT(*) c FROM bookings WHERE booking_date=:d AND status='waiting'", {'d': today})['c']
    done_today    = t_fetchone("SELECT COUNT(*) c FROM bookings WHERE booking_date=:d AND status='done'", {'d': today})['c']
    total_customers = t_fetchone("SELECT COUNT(*) c FROM customers")['c']
    vip_count = t_fetchone(
        "SELECT COUNT(*) c FROM customers WHERE is_vip=1 OR visit_count>=:v", {'v': VIP_VISIT_THRESHOLD}
    )['c']
    week_labels, week_counts = [], []
    for i in range(6,-1,-1):
        d = (date.today() - timedelta(days=i)).isoformat()
        week_labels.append(d[-5:])
        week_counts.append(t_fetchone("SELECT COUNT(*) c FROM bookings WHERE booking_date=:d", {'d': d})['c'])
    return render_template('dashboard.html', slug=slug,
        total_today=total_today, waiting_today=waiting_today, done_today=done_today,
        total_customers=total_customers, vip_count=vip_count, today=today,
        week_labels=week_labels, week_counts=week_counts)

# ─── Bookings (queue management) ───────────────────────────────

@app.route('/org/<slug>/bookings')
@load_tenant
@login_required
def bookings(slug):
    day = request.args.get('date', date.today().isoformat())
    rows = t_fetchall(
        "SELECT * FROM bookings WHERE booking_date=:d ORDER BY "
        "CASE status WHEN 'waiting' THEN 0 ELSE 1 END, queue_number",
        {'d': day}
    )
    return render_template('bookings.html', slug=slug, bookings=rows, day=day, today=date.today().isoformat())

@app.route('/org/<slug>/bookings/complete/<int:bid>', methods=['POST'])
@load_tenant
@login_required
def complete_booking(slug, bid):
    note = request.form.get('note','').strip()
    b = t_fetchone("SELECT * FROM bookings WHERE id=:id", {'id': bid})
    if not b:
        flash('الحجز غير موجود', 'warning')
        return redirect(url_for('bookings', slug=slug))
    t_execute(
        "UPDATE bookings SET status='done', notes=:n, completed_at=:c WHERE id=:id",
        {'n': note, 'c': datetime.utcnow().isoformat(), 'id': bid}
    )
    new_count = (t_fetchone("SELECT visit_count FROM customers WHERE id=:id", {'id': b['customer_id']}) or {}).get('visit_count', 0) + 1
    t_execute("UPDATE customers SET visit_count=:v WHERE id=:id", {'v': new_count, 'id': b['customer_id']})
    if new_count >= VIP_VISIT_THRESHOLD:
        t_execute("UPDATE customers SET is_vip=1 WHERE id=:id", {'id': b['customer_id']})
    flash('تم تسجيل انتهاء الحجز بنجاح', 'success')
    return redirect(url_for('bookings', slug=slug, date=b['booking_date']))

@app.route('/org/<slug>/bookings/cancel/<int:bid>', methods=['POST'])
@load_tenant
@login_required
def cancel_booking(slug, bid):
    b = t_fetchone("SELECT * FROM bookings WHERE id=:id", {'id': bid})
    if b:
        t_execute("UPDATE bookings SET status='cancelled' WHERE id=:id", {'id': bid})
        flash('تم إلغاء الحجز', 'warning')
        return redirect(url_for('bookings', slug=slug, date=b['booking_date']))
    return redirect(url_for('bookings', slug=slug))

# ─── Customers ──────────────────────────────────────────────────

@app.route('/org/<slug>/customers')
@load_tenant
@login_required
def customers(slug):
    search = request.args.get('search','').strip()
    sql = "SELECT * FROM customers WHERE 1=1"
    params = {}
    if search:
        sql += " AND (name ILIKE :s OR phone ILIKE :s)"
        params['s'] = f'%{search}%'
    sql += " ORDER BY visit_count DESC, name"
    rows = t_fetchall(sql, params)
    return render_template('customers.html', slug=slug, customers=rows, search=search,
                           vip_threshold=VIP_VISIT_THRESHOLD)

@app.route('/org/<slug>/customers/vip')
@load_tenant
@login_required
def vip_customers(slug):
    rows = t_fetchall(
        "SELECT * FROM customers WHERE is_vip=1 OR visit_count>=:v ORDER BY visit_count DESC, name",
        {'v': VIP_VISIT_THRESHOLD}
    )
    return render_template('vip_customers.html', slug=slug, customers=rows, vip_threshold=VIP_VISIT_THRESHOLD)

@app.route('/org/<slug>/customers/toggle_vip/<int:cid>', methods=['POST'])
@load_tenant
@login_required
def toggle_vip(slug, cid):
    c = t_fetchone("SELECT * FROM customers WHERE id=:id", {'id': cid})
    if not c:
        return jsonify({'success': False}), 404
    new_val = 0 if c['is_vip'] else 1
    t_execute("UPDATE customers SET is_vip=:v WHERE id=:id", {'v': new_val, 'id': cid})
    return jsonify({'success': True, 'is_vip': bool(new_val)})

# ─── Users (staff) ──────────────────────────────────────────────

@app.route('/org/<slug>/users')
@load_tenant
@login_required
@admin_required
def users(slug):
    rows = t_fetchall("SELECT * FROM users ORDER BY id")
    import json as _json
    for u in rows:
        try:
            u['permissions_list'] = _json.loads(u.get('permissions') or '[]')
        except Exception:
            u['permissions_list'] = []
    return render_template('users.html', slug=slug, users=rows, permissions=PERMISSIONS)

@app.route('/org/<slug>/users/add', methods=['GET','POST'])
@load_tenant
@login_required
@admin_required
def add_user(slug):
    if request.method == 'POST':
        import json as _json
        username = request.form.get('username','').strip()
        password = request.form.get('password','').strip()
        name     = request.form.get('name','').strip()
        role     = request.form.get('role','barber')
        perms    = request.form.getlist('permissions')
        if not all([username, password, name]):
            flash('يرجى تعبئة جميع الحقول', 'warning')
            return redirect(url_for('add_user', slug=slug))
        existing = t_fetchone("SELECT id FROM users WHERE username=:u", {'u': username})
        if existing:
            flash('اسم المستخدم مستخدم بالفعل', 'warning')
            return redirect(url_for('add_user', slug=slug))
        t_execute(
            "INSERT INTO users (username,password,name,role,permissions,is_active) VALUES (:u,:p,:n,:r,:pm,1)",
            {'u': username, 'p': password, 'n': name, 'r': role, 'pm': _json.dumps(perms)}
        )
        flash('تم إضافة المستخدم بنجاح', 'success')
        return redirect(url_for('users', slug=slug))
    return render_template('user_form.html', slug=slug, action='add', user=None, permissions=PERMISSIONS)

@app.route('/org/<slug>/users/edit/<int:uid>', methods=['GET','POST'])
@load_tenant
@login_required
@admin_required
def edit_user(slug, uid):
    import json as _json
    user = t_fetchone("SELECT * FROM users WHERE id=:id", {'id': uid})
    if not user:
        flash('المستخدم غير موجود', 'warning')
        return redirect(url_for('users', slug=slug))
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','').strip()
        name     = request.form.get('name','').strip()
        role     = request.form.get('role','barber')
        perms    = request.form.getlist('permissions')
        if password:
            t_execute(
                "UPDATE users SET username=:u,password=:p,name=:n,role=:r,permissions=:pm WHERE id=:id",
                {'u': username, 'p': password, 'n': name, 'r': role, 'pm': _json.dumps(perms), 'id': uid}
            )
        else:
            t_execute(
                "UPDATE users SET username=:u,name=:n,role=:r,permissions=:pm WHERE id=:id",
                {'u': username, 'n': name, 'r': role, 'pm': _json.dumps(perms), 'id': uid}
            )
        flash('تم تحديث بيانات المستخدم', 'success')
        return redirect(url_for('users', slug=slug))
    user['permissions_list'] = _json.loads(user.get('permissions') or '[]')
    return render_template('user_form.html', slug=slug, action='edit', user=user, permissions=PERMISSIONS)

@app.route('/org/<slug>/users/delete/<int:uid>', methods=['POST'])
@load_tenant
@login_required
@admin_required
def delete_user(slug, uid):
    if uid == session.get('user_id'):
        flash('لا يمكنك حذف حسابك الخاص', 'warning')
    else:
        t_execute("DELETE FROM users WHERE id=:id", {'id': uid})
        flash('تم حذف المستخدم', 'success')
    return redirect(url_for('users', slug=slug))

@app.route('/org/<slug>/users/toggle/<int:uid>', methods=['POST'])
@load_tenant
@login_required
@admin_required
def toggle_user(slug, uid):
    u = t_fetchone("SELECT * FROM users WHERE id=:id", {'id': uid})
    if not u:
        return jsonify({'success': False}), 404
    new_val = 0 if u['is_active'] else 1
    t_execute("UPDATE users SET is_active=:v WHERE id=:id", {'v': new_val, 'id': uid})
    return jsonify({'success': True, 'is_active': bool(new_val)})

# ─── Settings ───────────────────────────────────────────────────

@app.route('/org/<slug>/settings', methods=['GET','POST'])
@load_tenant
@login_required
@admin_required
def settings(slug):
    s = t_fetchone("SELECT * FROM system_settings LIMIT 1")
    if request.method == 'POST':
        if request.form.get('form_type') == 'activate_subscription':
            code_str = request.form.get('activation_code','').strip().upper()
            sub_code = SubscriptionCode.query.filter_by(code=code_str, slug=slug, is_used=False).first()
            if not sub_code:
                flash('الكود غير صحيح أو غير صالح لهذا الصالون', 'danger')
            else:
                base = g.tenant.subscription_until if g.tenant.is_subscribed() else datetime.utcnow()
                g.tenant.subscription_until = base + timedelta(days=30 * sub_code.months)
                sub_code.is_used = True
                sub_code.used_at = datetime.utcnow()
                main_db.session.commit()
                flash(f'تم تفعيل الاشتراك بنجاح لمدة {sub_code.months} شهر', 'success')
            return redirect(url_for('settings', slug=slug))
        elif request.form.get('form_type') == 'add_service':
            name  = request.form.get('service_name','').strip()
            price = request.form.get('service_price','0').strip() or '0'
            if name:
                t_execute("INSERT INTO services (name, price, created_at) VALUES (:n,:p,:d)",
                          {'n': name, 'p': price, 'd': datetime.utcnow().isoformat()})
                flash('تمت إضافة الخدمة', 'success')
            return redirect(url_for('settings', slug=slug))
        elif request.form.get('form_type') == 'delete_service':
            sid = request.form.get('service_id')
            t_execute("DELETE FROM services WHERE id=:id", {'id': sid})
            flash('تم حذف الخدمة', 'success')
            return redirect(url_for('settings', slug=slug))
        t_execute(
            "UPDATE system_settings SET system_name=:n,system_subtitle=:s,salon_name=:sn,salon_address=:sa,admin_phone=:ap,updated_at=:u",
            {'n':request.form.get('system_name','').strip(),
             's':request.form.get('system_subtitle','').strip(),
             'sn':request.form.get('salon_name','').strip(),
             'sa':request.form.get('salon_address','').strip(),
             'ap':request.form.get('admin_phone','').strip(),
             'u':datetime.utcnow().isoformat()}
        )
        flash('تم حفظ الإعدادات بنجاح','success')
        return redirect(url_for('settings', slug=slug))
    cc = t_fetchone("SELECT COUNT(*) c FROM customers")['c']
    uc = t_fetchone("SELECT COUNT(*) c FROM users")['c']
    bc = t_fetchone("SELECT COUNT(*) c FROM bookings")['c']
    services_list = t_fetchall("SELECT * FROM services ORDER BY id")
    booking_link = url_for('book', slug=slug, _external=True)
    return render_template('settings.html', slug=slug, settings=s,
                           customers_count=cc, users_count=uc, bookings_count=bc,
                           is_subscribed=g.is_subscribed, subscription_until=g.tenant.subscription_until,
                           free_max_users=FREE_PLAN_MAX_USERS, free_max_customers=FREE_PLAN_MAX_CUSTOMERS,
                           subscription_plans=SUBSCRIPTION_PLANS, whatsapp_number=SUBSCRIPTION_WHATSAPP,
                           services=services_list, booking_link=booking_link)

# ─── API ──────────────────────────────────────────────────────

@app.route('/org/<slug>/api/settings')
@load_tenant
@login_required
def api_settings(slug):
    s = t_fetchone("SELECT * FROM system_settings LIMIT 1")
    return jsonify({
        'system_name':     s['system_name']     if s else '',
        'system_subtitle': s['system_subtitle'] if s else '',
        'salon_name':      s['salon_name']      if s else ''
    })

@app.route('/org/<slug>/api/notifications')
@load_tenant
@login_required
def api_notifications(slug):
    today   = date.today().isoformat()
    waiting = t_fetchone("SELECT COUNT(*) c FROM bookings WHERE booking_date=:d AND status='waiting'", {'d':today})['c']
    total   = t_fetchone("SELECT COUNT(*) c FROM customers")['c']
    notes = []
    if waiting > 0: notes.append({'type':'info','title':'حجوزات بالانتظار','message':f'{waiting} عميل بالدور الآن','icon':'late'})
    notes.append({'type':'success','title':'العملاء المسجلون','message':f'إجمالي {total} عميل في النظام','icon':'students'})
    return jsonify({'notifications':notes,'count':sum(1 for n in notes if n['type'] in ('warning','danger'))})

# ══════════════════════════════════════════════════════════════
#  مطور النظام — صفحات مخفية
#  الدخول من: /system/developer-login
# ══════════════════════════════════════════════════════════════

@app.route('/system/developer-login', methods=['GET','POST'])
def developer_login():
    error = None
    if request.method == 'POST':
        u = request.form.get('username','').strip()
        p = request.form.get('password','').strip()
        if u == DEVELOPER_USERNAME and p == DEVELOPER_PASSWORD:
            session.clear()
            session['is_developer'] = True
            session['dev_name']     = 'محمد ناصر'
            return redirect(url_for('developer_dashboard'))
        error = 'بيانات الدخول غير صحيحة'
    return render_template('developer_login.html', error=error)

@app.route('/system/developer-logout')
def developer_logout():
    session.clear()
    return redirect(url_for('developer_login'))

@app.route('/system/developer-dashboard')
@developer_required
def developer_dashboard():
    tenants = Tenant.query.order_by(Tenant.created_at.desc()).all()
    return render_template('developer_dashboard.html', tenants=tenants)

@app.route('/system/developer-toggle/<slug>', methods=['POST'])
@developer_required
def developer_toggle_tenant(slug):
    tenant = Tenant.query.filter_by(slug=slug).first()
    if tenant:
        tenant.is_active = not tenant.is_active
        main_db.session.commit()
        return jsonify({'success': True, 'is_active': tenant.is_active})
    return jsonify({'success': False}), 404

@app.route('/system/developer-delete/<slug>', methods=['POST'])
@developer_required
def developer_delete_tenant(slug):
    tenant = Tenant.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({'success': False}), 404
    SubscriptionCode.query.filter_by(slug=slug).delete()
    drop_tenant_schema(slug)
    main_db.session.delete(tenant)
    main_db.session.commit()
    return jsonify({'success': True})

@app.route('/system/developer-subscriptions', methods=['GET','POST'])
@developer_required
def developer_subscriptions():
    if request.method == 'POST':
        slug   = request.form.get('slug','').strip()
        months = int(request.form.get('months', 0) or 0)
        tenant = Tenant.query.filter_by(slug=slug).first()
        if not tenant or months <= 0:
            flash('بيانات غير صحيحة', 'danger')
        else:
            code = secrets.token_hex(4).upper()
            sc = SubscriptionCode(code=code, slug=slug, months=months)
            main_db.session.add(sc)
            main_db.session.commit()
            flash(f'تم إنشاء الكود: {code} لصالون {tenant.org_name} لمدة {months} شهر', 'success')
        return redirect(url_for('developer_subscriptions'))
    tenants = Tenant.query.order_by(Tenant.org_name).all()
    codes   = SubscriptionCode.query.order_by(SubscriptionCode.created_at.desc()).all()
    tenant_map = {t.slug: t for t in tenants}
    return render_template('developer_subscriptions.html', tenants=tenants, codes=codes, tenant_map=tenant_map)

@app.route('/system/developer-enter/<slug>')
@developer_required
def developer_enter(slug):
    tenant = Tenant.query.filter_by(slug=slug).first()
    if not tenant:
        return render_template('404.html'), 404
    session['user_id']     = 0
    session['user_name']   = 'محمد ناصر (مطور النظام)'
    session['user_role']   = 'admin'
    session['tenant_slug'] = slug
    session['is_developer']= True
    return redirect(url_for('dashboard', slug=slug))

# ══════════════════════════════════════════════════════════════
#  Init — Migration تلقائية لجداول الـ public schema الرئيسية
# ══════════════════════════════════════════════════════════════

def init_app():
    try:
        with app.app_context():
            main_db.create_all()  # ينشئ tenants / subscription_codes في public إن لم تكن موجودة
    except Exception as exc:
        # لا نمنع الـ worker من الإقلاع بسبب فشل مؤقت في الاتصال بقاعدة
        # البيانات (مثال: متغير DATABASE_URL غير مضبوط بعد أو القاعدة لم
        # تصبح جاهزة بعد). سيُعاد المحاولة تلقائياً عند أول طلب فعلي.
        app.logger.error(f"init_app: failed to create tables on boot: {exc}")

init_app()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
