from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from api.models import AuditLog, Category, Customer, Order, OrderItem, Product, Review


class Command(BaseCommand):
    help = "Migrate and seed test product records for default, slave1, slave2, and slave3 databases"

    def handle(self, *args, **options):
        db_aliases = list(settings.DATABASES.keys())
        self.stdout.write(f"Setting up databases: {db_aliases}")

        # Ensure target databases exist in PostgreSQL
        try:
            import psycopg2
            from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
            for alias in db_aliases:
                db_cfg = settings.DATABASES.get(alias, {})
                if db_cfg.get("ENGINE") == "django.db.backends.postgresql":
                    target_db_name = db_cfg.get("NAME")
                    host = db_cfg.get("HOST", "postgres")
                    port = db_cfg.get("PORT", "5432")
                    user = db_cfg.get("USER", "django")
                    password = db_cfg.get("PASSWORD", "django")
                    try:
                        conn = psycopg2.connect(
                            dbname="postgres",
                            user=user,
                            password=password,
                            host=host,
                            port=port,
                        )
                        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                        with conn.cursor() as cur:
                            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db_name,))
                            if not cur.fetchone():
                                self.stdout.write(f"Creating database '{target_db_name}' on {host}:{port}...")
                                cur.execute(f'CREATE DATABASE "{target_db_name}"')
                        conn.close()
                    except Exception:
                        # Database may already exist or connecting via default db name
                        pass
        except Exception as e:
            self.stderr.write(f"Notice: could not auto-create databases ({e}); continuing.")

        for db in db_aliases:
            self.stdout.write(f"Running migrations for database '{db}'...")
            try:
                call_command("migrate", database=db, interactive=False)
                self.stdout.write(self.style.SUCCESS(f"Migrations complete for '{db}'"))
            except Exception as e:
                self.stderr.write(f"Error migrating database '{db}': {e}")
                continue

            # Seed categories
            if Category.objects.using(db).count() == 0:
                cat_elec = Category.objects.using(db).create(
                    name="Electronics", slug="electronics", description="Gadgets and devices"
                )
                cat_books = Category.objects.using(db).create(
                    name="Books", slug="books", description="Technical and fiction books"
                )
                Category.objects.using(db).create(
                    name="Home & Kitchen", slug="home-kitchen", description="Appliances and tools"
                )
                self.stdout.write(self.style.SUCCESS(f"Seeded categories in '{db}'"))
            else:
                cat_elec = Category.objects.using(db).filter(slug="electronics").first()
                cat_books = Category.objects.using(db).filter(slug="books").first()

            # Seed customers
            if Customer.objects.using(db).count() == 0:
                alice = Customer.objects.using(db).create(
                    name="Alice Smith", email=f"alice@{db}.test", tier="gold"
                )
                bob = Customer.objects.using(db).create(
                    name="Bob Jones", email=f"bob@{db}.test", tier="silver"
                )
                Customer.objects.using(db).create(
                    name="Charlie Brown", email=f"charlie@{db}.test", tier="bronze"
                )
                Customer.objects.using(db).create(
                    name="Diana Prince", email=f"diana@{db}.test", tier="platinum"
                )
                self.stdout.write(self.style.SUCCESS(f"Seeded customers in '{db}'"))
            else:
                alice = Customer.objects.using(db).first()
                bob = Customer.objects.using(db).last()

            # Seed product data if none exists
            count = Product.objects.using(db).count()
            if count == 0:
                self.stdout.write(f"Seeding sample products for database '{db}'...")
                p1 = Product.objects.using(db).create(
                    name=f"Product from {db.upper()}",
                    description=f"Sample product record residing in the {db} database",
                    price="29.99",
                    stock=100,
                    category=cat_elec,
                )
                p2 = Product.objects.using(db).create(
                    name=f"Item {db.upper()}-002",
                    description=f"Secondary test item residing in {db}",
                    price="49.50",
                    stock=50,
                    category=cat_books,
                )
                self.stdout.write(self.style.SUCCESS(f"Seeded sample products in '{db}'"))
            else:
                p1 = Product.objects.using(db).first()
                p2 = Product.objects.using(db).last()
                self.stdout.write(f"Database '{db}' already contains {count} products.")

            # Seed orders
            if Order.objects.using(db).count() == 0 and alice and p1:
                o1 = Order.objects.using(db).create(
                    customer=alice,
                    status="completed",
                    total_amount="59.98",
                    shipping_address="123 Tech Lane, Cloud City",
                )
                OrderItem.objects.using(db).create(order=o1, product=p1, quantity=2, unit_price="29.99")
                if bob and p2:
                    o2 = Order.objects.using(db).create(
                        customer=bob,
                        status="pending",
                        total_amount="49.50",
                        shipping_address="456 Dev Ave, Server Town",
                    )
                    OrderItem.objects.using(db).create(order=o2, product=p2, quantity=1, unit_price="49.50")
                self.stdout.write(self.style.SUCCESS(f"Seeded sample orders in '{db}'"))

            # Seed review & audit log
            if Review.objects.using(db).count() == 0 and alice and p1:
                Review.objects.using(db).create(
                    product=p1,
                    customer=alice,
                    rating=5,
                    comment="Excellent performance under load!",
                )
            if AuditLog.objects.using(db).count() == 0:
                AuditLog.objects.using(db).create(
                    action="system.bootstrap",
                    entity_type="Database",
                    entity_id=db,
                    details=f"Seeded initial tables on {db}",
                )

        self.stdout.write(self.style.SUCCESS("All databases setup successfully!"))
