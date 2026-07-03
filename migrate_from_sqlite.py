"""
سكربت ترحيل البيانات من الإصدار القديم (SQLite) إلى PostgreSQL
================================================================
يُستخدم مرة واحدة فقط عند الانتقال من نسخة SQLite القديمة (data/main.db +
data/tenants/<slug>.db) إلى نسخة PostgreSQL الجديدة (schema لكل مؤسسة).

طريقة الاستخدام:
    1. تأكد إن متغير البيئة DATABASE_URL مضبوط على قاعدة PostgreSQL الجديدة.
    2. ضع مجلد "data" القديم (اللي فيه main.db و tenants/) بجانب هذا الملف،
       أو حدد مساره عبر متغير البيئة OLD_SQLITE_DATA_DIR.
    3. شغّل: python migrate_from_sqlite.py

السكربت آمن للتشغيل أكثر من مرة (يتجاهل السجلات الموجودة بالفعل).
لو مفيش قاعدة SQLite قديمة أصلاً (تنصيب جديد بالكامل)، السكربت هيطبع
رسالة ويخرج بدون أي تغيير — مفيش داعي لتشغيله في هذه الحالة.
"""

import os
import sqlite3
import sys
from datetime import datetime

# نستورد من app.py لإعادة استخدام نفس الإعدادات والاتصال بـ PostgreSQL
from app import (
    app, main_db, Tenant, SubscriptionCode,
    tenant_engine, ensure_tenant_schema, tenant_schema_name, TENANT_SCHEMA,
)
from sqlalchemy import text

OLD_DATA_DIR = os.environ.get('OLD_SQLITE_DATA_DIR', os.path.join(os.path.dirname(__file__), 'data'))
OLD_MAIN_DB  = os.path.join(OLD_DATA_DIR, 'main.db')
OLD_TENANTS_DIR = os.path.join(OLD_DATA_DIR, 'tenants')


def sqlite_rows(db_path, table):
    """يقرأ كل صفوف جدول من ملف SQLite كقواميس."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def migrate_main_db():
    """ترحيل جدول المؤسسات وأكواد الاشتراك من main.db القديم إلى public.tenants."""
    if not os.path.exists(OLD_MAIN_DB):
        print(f'⚠️  لم يتم العثور على {OLD_MAIN_DB} — سيتم تخطي ترحيل بيانات المؤسسات الرئيسية.')
        return []

    print(f'📥 قراءة المؤسسات من {OLD_MAIN_DB} ...')
    tenants_rows = sqlite_rows(OLD_MAIN_DB, 'tenants')
    codes_rows   = sqlite_rows(OLD_MAIN_DB, 'subscription_codes')

    migrated_slugs = []
    with app.app_context():
        for row in tenants_rows:
            existing = Tenant.query.filter_by(slug=row['slug']).first()
            if existing:
                print(f"  ↷  المؤسسة '{row['slug']}' موجودة بالفعل في PostgreSQL — تخطي")
                migrated_slugs.append(row['slug'])
                continue
            tenant = Tenant(
                slug=row['slug'],
                org_name=row['org_name'],
                owner_name=row['owner_name'],
                owner_email=row['owner_email'],
                owner_phone=row.get('owner_phone'),
                owner_password=row['owner_password'],
                is_active=bool(row.get('is_active', 1)),
                subscription_until=_parse_dt(row.get('subscription_until')),
                created_at=_parse_dt(row.get('created_at')) or datetime.utcnow(),
            )
            main_db.session.add(tenant)
            migrated_slugs.append(row['slug'])
            print(f"  ✔  تم ترحيل المؤسسة '{row['slug']}' ({row['org_name']})")
        main_db.session.commit()

        for row in codes_rows:
            existing = SubscriptionCode.query.filter_by(code=row['code']).first()
            if existing:
                continue
            sc = SubscriptionCode(
                code=row['code'],
                slug=row['slug'],
                months=row['months'],
                is_used=bool(row.get('is_used', 0)),
                used_at=_parse_dt(row.get('used_at')),
                created_at=_parse_dt(row.get('created_at')) or datetime.utcnow(),
            )
            main_db.session.add(sc)
        main_db.session.commit()
        if codes_rows:
            print(f'  ✔  تم ترحيل {len(codes_rows)} كود اشتراك')

    return migrated_slugs


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for pattern in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(str(value), pattern)
        except ValueError:
            continue
    return None


def migrate_tenant(slug):
    """ينشئ Schema المؤسسة في PostgreSQL (لو مش موجودة) وينسخ كل جداولها
    من ملف SQLite القديم الخاص بها."""
    db_path = os.path.join(OLD_TENANTS_DIR, f'{slug}.db')
    if not os.path.exists(db_path):
        print(f"  ⚠️  لا يوجد ملف SQLite لمؤسسة '{slug}' في {db_path} — تخطي")
        return

    print(f"📦 ترحيل بيانات المؤسسة '{slug}' من {db_path} ...")
    ensure_tenant_schema(slug)
    schema = tenant_schema_name(slug)

    tables_columns = {
        'users':           ['id','username','password','name','role','permissions','is_active'],
        'system_settings': ['id','system_name','system_subtitle','salon_name','salon_address','admin_phone','updated_at'],
        'services':        ['id','name','price','created_at'],
        'customers':       ['id','name','phone','visit_count','is_vip','created_at'],
        'bookings':        ['id','customer_id','customer_name','customer_phone','service_id','service_name',
                             'queue_number','booking_date','status','notes','created_at','completed_at'],
    }

    with tenant_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}"'))
        for table, cols in tables_columns.items():
            rows = sqlite_rows(db_path, table)
            if not rows:
                continue
            copied = 0
            for row in rows:
                # نتجاهل أي أعمدة زيادة موجودة في SQLite القديم وغير معروفة هنا
                data = {c: row.get(c) for c in cols}
                col_list = ', '.join(cols)
                placeholders = ', '.join(f':{c}' for c in cols)
                try:
                    conn.execute(
                        text(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                             f"ON CONFLICT (id) DO NOTHING"),
                        data
                    )
                    copied += 1
                except Exception as e:
                    print(f"    ✗ خطأ أثناء نسخ صف من {table}: {e}")
            print(f"  ✔  {table}: تم نسخ {copied} صف")
            # إعادة ضبط sequence الخاص بـ SERIAL بعد الإدخال اليدوي للـ id
            conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
            ))


def main():
    print('=' * 60)
    print('ترحيل البيانات من SQLite (القديم) إلى PostgreSQL (الجديد)')
    print('=' * 60)

    if not os.path.exists(OLD_DATA_DIR):
        print(f"لا يوجد مجلد بيانات قديم في '{OLD_DATA_DIR}'.")
        print('لا يوجد شيء لترحيله — يمكنك تجاهل هذا السكربت في التنصيبات الجديدة.')
        sys.exit(0)

    slugs = migrate_main_db()

    if os.path.isdir(OLD_TENANTS_DIR):
        # أيضاً نلتقط أي ملفات .db موجودة فعلياً حتى لو مش مسجلة في main.db
        found_files = [f[:-3] for f in os.listdir(OLD_TENANTS_DIR) if f.endswith('.db')]
        all_slugs = sorted(set(slugs) | set(found_files))
    else:
        all_slugs = slugs

    if not all_slugs:
        print('لا توجد مؤسسات لترحيلها.')
    else:
        for slug in all_slugs:
            migrate_tenant(slug)

    print('=' * 60)
    print('✅ انتهى الترحيل.')
    print('=' * 60)


if __name__ == '__main__':
    main()
