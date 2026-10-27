import os, sqlite3, json, io, secrets, logging
from datetime import date, datetime
from functools import wraps
from urllib.parse import quote
from flask import Flask, g, request, redirect, url_for, session, flash, render_template_string, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash

BASE=os.path.dirname(os.path.abspath(__file__))
DB=os.environ.get("EASYKADA_DATABASE_PATH", os.path.join(BASE,"instance","easykada.sqlite3"))
app=Flask(__name__); app.secret_key=os.environ.get("EASYKADA_SECRET_KEY",secrets.token_hex(32))
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Lax")
SCHEMA="""
CREATE TABLE IF NOT EXISTS shops(id INTEGER PRIMARY KEY,name TEXT NOT NULL,username TEXT UNIQUE NOT NULL,active INTEGER NOT NULL DEFAULT 1,phone TEXT DEFAULT '',address TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL,shop_id INTEGER,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY,shop_id INTEGER,name TEXT NOT NULL,code TEXT DEFAULT '',category TEXT DEFAULT '',unit TEXT DEFAULT 'pcs',price REAL DEFAULT 0,cost REAL DEFAULT 0,stock REAL DEFAULT 0,low_stock REAL DEFAULT 5,barcode TEXT DEFAULT '',active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY,shop_id INTEGER,name TEXT NOT NULL,phone TEXT DEFAULT '',whatsapp TEXT DEFAULT '',address TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS suppliers(id INTEGER PRIMARY KEY,shop_id INTEGER,name TEXT NOT NULL,phone TEXT DEFAULT '',address TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY,shop_id INTEGER,invoice TEXT NOT NULL,sale_date TEXT,sale_time TEXT,customer_id INTEGER,customer_name TEXT,subtotal REAL,discount REAL,total REAL,payment_method TEXT,received REAL,due REAL,cost_total REAL,UNIQUE(shop_id,invoice));
CREATE TABLE IF NOT EXISTS sale_items(id INTEGER PRIMARY KEY,sale_id INTEGER,product_id INTEGER,product_name TEXT,unit TEXT,qty REAL,price REAL,cost REAL,line_total REAL);
CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY,shop_id INTEGER,purchase_date TEXT,supplier_id INTEGER,supplier_name TEXT,product_id INTEGER,product_name TEXT,qty REAL,unit_cost REAL,total REAL,paid REAL,due REAL,notes TEXT DEFAULT '',invoice_no TEXT DEFAULT '',batch_no TEXT DEFAULT '',expiry_date TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS payment_methods(id INTEGER PRIMARY KEY,shop_id INTEGER,name TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,UNIQUE(shop_id,name));
CREATE TABLE IF NOT EXISTS sales_returns(id INTEGER PRIMARY KEY,shop_id INTEGER,return_no TEXT,return_date TEXT,sale_id INTEGER,customer_name TEXT,reason TEXT,total REAL,notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS sales_return_items(id INTEGER PRIMARY KEY,return_id INTEGER,product_id INTEGER,product_name TEXT,qty REAL,unit_price REAL,line_total REAL,cost REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS purchase_returns(id INTEGER PRIMARY KEY,shop_id INTEGER,return_no TEXT,return_date TEXT,purchase_id INTEGER,supplier_name TEXT,reason TEXT,total REAL,notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS purchase_return_items(id INTEGER PRIMARY KEY,return_id INTEGER,product_id INTEGER,product_name TEXT,qty REAL,unit_cost REAL,line_total REAL);
CREATE TABLE IF NOT EXISTS purchase_payments(id INTEGER PRIMARY KEY,shop_id INTEGER,purchase_id INTEGER,payment_date TEXT,amount REAL,method TEXT,notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY,shop_id INTEGER,entry_date TEXT,category TEXT,amount REAL,payment_method TEXT,notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS incomes(id INTEGER PRIMARY KEY,shop_id INTEGER,entry_date TEXT,category TEXT,amount REAL,payment_method TEXT,notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY,shop_id INTEGER,kind TEXT,name TEXT,UNIQUE(shop_id,kind,name));
"""
def dbconn():
 os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
 c=sqlite3.connect(DB, timeout=20); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=20000"); return c
def init():
 os.makedirs(os.path.dirname(DB),exist_ok=True); c=dbconn(); c.executescript(SCHEMA)
 # Safe in-place migrations for existing Render databases.
 cols={r[1] for r in c.execute("PRAGMA table_info(purchases)")}
 for col,typ in [("invoice_no","TEXT DEFAULT ''"),("batch_no","TEXT DEFAULT ''"),("expiry_date","TEXT DEFAULT ''")]:
  if col not in cols:c.execute(f"ALTER TABLE purchases ADD COLUMN {col} {typ}")
 rcols={r[1] for r in c.execute("PRAGMA table_info(sales_return_items)")}
 if "cost" not in rcols:c.execute("ALTER TABLE sales_return_items ADD COLUMN cost REAL DEFAULT 0")
 c.execute("UPDATE purchases SET invoice_no='PUR-'||printf('%06d',id) WHERE invoice_no IS NULL OR invoice_no='' ")
 if not c.execute("SELECT id FROM users WHERE username='admin'").fetchone():
  c.execute("INSERT INTO users(username,password_hash,role,active) VALUES(?,?,?,1)",("admin",generate_password_hash("admin123"),"admin"))
 # Seed common payment methods per existing shop.
 for sh in c.execute("SELECT id FROM shops").fetchall():
  for method in ("Cash","UPI","Card","Bank Transfer","Credit","Other"):
   c.execute("INSERT OR IGNORE INTO payment_methods(shop_id,name) VALUES(?,?)",(sh[0],method))
 c.commit(); c.close()
def db():
 if "db" not in g:g.db=dbconn()
 return g.db
@app.teardown_appcontext
def close_db(e=None):
 c=g.pop("db",None)
 if c:c.close()
def today():return date.today().isoformat()

# Make today's date available to every inline Jinja template, including /entries.
@app.context_processor
def inject_template_helpers():
 return {"today":today}

def now():return datetime.now().isoformat(timespec="seconds")
def val(k,d=""):return (request.form.get(k) or d).strip()
def num(k,d=0):
 try:return float(request.form.get(k,d) or d)
 except:return float(d)
def shopid():return int(session["shop_id"])
def rows(sql,args=()):return db().execute(sql,(shopid(),*args)).fetchall()
def getp(pid):return db().execute("SELECT * FROM products WHERE id=? AND shop_id=?",(pid,shopid())).fetchone()
def fmt(v):return f"₹{float(v or 0):,.2f}"
app.jinja_env.filters["money"]=fmt
def logged(f):
 @wraps(f)
 def w(*a,**kw):
  if not session.get("user_id"):return redirect(url_for("login"))
  return f(*a,**kw)
 return w
def adminonly(f):
 @wraps(f)
 def w(*a,**kw):
  if not session.get("user_id"):return redirect(url_for("login"))
  if session.get("role")!="admin":abort(403)
  return f(*a,**kw)
 return w
def shoponly(f):
 @wraps(f)
 def w(*a,**kw):
  if not session.get("user_id"):return redirect(url_for("login"))
  if session.get("role")!="shop":return redirect(url_for("admin"))
  sh=db().execute("SELECT * FROM shops WHERE id=? AND active=1",(session.get("shop_id"),)).fetchone()
  if not sh:session.clear();flash("Shop is inactive.","danger");return redirect(url_for("login"))
  g.shop=sh
  return f(*a,**kw)
 return w
CSS="""body{background:#f4f7fb;color:#1f2937;font:14px system-ui}.nav{background:#126b4f;color:white;padding:13px 20px;display:flex;justify-content:space-between;align-items:center}.nav a{color:white;text-decoration:none;font-weight:700;font-size:20px}.layout{display:grid;grid-template-columns:220px 1fr;gap:18px;padding:18px}.side{display:flex;flex-direction:column;gap:4px}.side a{background:white;border-radius:8px;padding:11px;text-decoration:none;color:#234}.side a:hover{background:#e6f6ee}.main{min-width:0}.panel,.metric{background:white;border:1px solid #e2e8f0;border-radius:13px;padding:16px;margin-bottom:15px;box-shadow:0 3px 12px #00000008}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.metric b{display:block;font-size:23px;margin-top:5px}.muted{color:#667085}.formgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}.field label{display:block;font-size:12px;margin-bottom:4px;color:#667085}.field input,.field select,input,select,textarea{max-width:100%;box-sizing:border-box}.field input,.field select,input,select,textarea{border:1px solid #ccd5df;border-radius:7px;padding:9px;width:100%;background:white}.btn,button{border:0;border-radius:7px;padding:9px 12px;cursor:pointer;background:#126b4f;color:white;text-decoration:none;display:inline-block;font-size:13px}.btn2{background:#edf2f7;color:#263238}.danger{background:#b42318}.warn{background:#b7791f}.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:620px}th,td{text-align:left;padding:9px;border-bottom:1px solid #e6ebf1;vertical-align:middle}th{font-size:12px;color:#667085;white-space:nowrap}.actions{display:flex;gap:4px;flex-wrap:wrap}.flash{padding:10px;border-radius:8px;margin:10px 18px;background:#e6f6ee}.flash.danger{background:#fee4e2}.flash.warning{background:#fff0c2}.login{max-width:400px;margin:8vh auto;padding:24px;background:white;border-radius:14px}.products{display:grid;grid-template-columns:repeat(auto-fill,minmax(135px,1fr));gap:8px}.tile{background:#f8fbfd;border:1px solid #dbe4ec;border-radius:8px;padding:10px;text-align:left;color:#123}.tile b,.tile span,.tile small{display:block;margin-bottom:4px}.tile span{color:#126b4f}.total{font-size:28px;color:#126b4f;font-weight:800}details{margin-top:8px}summary{cursor:pointer;color:#126b4f}@media(max-width:850px){.layout{grid-template-columns:1fr;padding:10px}.side{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px}.side a{font-size:12px;padding:9px}.panel{padding:12px}.metric b{font-size:20px}.login{margin:4vh 12px}.formgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:420px){.formgrid{grid-template-columns:1fr}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}"""
BASE="""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{{ title or 'EasyKada' }}</title><style>"""+CSS+"""</style></head><body><div class=nav><a href="{{ url_for('index') }}">🛒 EasyKada</a>{% if session.get('username') %}<span>{{session.username}} · <a style='font-size:13px' href="{{url_for('logout')}}">Logout</a></span>{% endif %}</div>{% for cat,msg in get_flashed_messages(with_categories=true) %}<div class='flash {{cat}}'>{{msg}}</div>{% endfor %}{% if session.get('role')=='shop' %}<div class=layout><nav class=side>{% for ep,label in [('dashboard','Dashboard'),('pos','New Sale / POS'),('products','Products & Stock'),('history','Sales History & Bills'),('purchases','Purchases'),('entries','Other Income/Expenses'),('sales_returns','Sales Returns'),('purchase_returns','Purchase Returns'),('payment_methods','Payment Methods'),('customers','Customers'),('suppliers','Suppliers'),('reports','Accounts & Reports'),('backup','Backup')] %}<a href="{{url_for(ep)}}">{{label}}</a>{% endfor %}</nav><main class=main>{% else %}<main style='max-width:1250px;margin:auto;padding:18px'>{% endif %}"""
END="""</main>{% if session.get('role')=='shop' %}</div>{% endif %}<script>function confirmDelete(msg){return confirm(msg)}function filterTable(id,q){document.querySelectorAll('#'+id+' tbody tr').forEach(r=>r.style.display=r.innerText.toLowerCase().includes(q.toLowerCase())?'':'none')}</script></body></html>"""
def page(content,title="EasyKada"):
 return render_template_string(BASE+content+END,title=title)
@app.route("/healthz")
def healthz():
 try:
  c=dbconn(); c.execute("SELECT 1").fetchone(); c.close()
  return {"status":"ok"},200
 except Exception:
  logging.exception("EasyKada health check failed")
  return {"status":"error"},500
@app.route("/")
def index():return redirect(url_for("login") if not session.get("user_id") else url_for("admin" if session["role"]=="admin" else "dashboard"))
@app.route("/login",methods=["GET","POST"])
def login():
 if request.method=="POST":
  u=db().execute("SELECT * FROM users WHERE username=?",(val("username").strip(),)).fetchone()
  if not u or not u["active"] or not check_password_hash(u["password_hash"],request.form.get("password","")):flash("Invalid or inactive login.","danger")
  elif u["role"]=="shop" and not db().execute("SELECT id FROM shops WHERE id=? AND active=1",(u["shop_id"],)).fetchone():flash("Shop is deactivated.","danger")
  else:
   session.clear();session.update(user_id=u["id"],username=u["username"],role=u["role"],shop_id=u["shop_id"])
   return redirect(url_for("admin" if u["role"]=="admin" else "dashboard"))
 return page("""<div class=login><h1>EasyKada</h1><p class=muted>Multi-shop grocery manager</p><form method=post><div class=field><label>Username</label><input name=username required></div><div class=field><label>Password</label><input name=password type=password required></div><p><button>Login</button></p></form><small>First login: admin / admin123. Change this immediately.</small></div>""","Login")
@app.route("/logout")
def logout():session.clear();return redirect(url_for("login"))
@app.route("/admin")
@adminonly
def admin():
 shops=db().execute("SELECT s.*,u.id user_id FROM shops s LEFT JOIN users u ON u.shop_id=s.id AND u.role='shop' ORDER BY s.id DESC").fetchall()
 content="""<h1>Admin Console</h1><div class=grid><section class=panel><h2>Create shop</h2><form method=post action=/admin/create><div class=formgrid><div class=field><label>Shop name</label><input name=name required></div><div class=field><label>Username</label><input name=username required></div><div class=field><label>Password (8+ chars)</label><input type=password name=password minlength=8 required></div><div class=field><label>Phone</label><input name=phone></div><div class=field><label>Address</label><input name=address></div></div><p><button>Create shop</button></p></form></section><section class=panel><h2>Change admin login</h2><form method=post action=/admin/password><div class=field><label>New username</label><input name=username value='{{ session.username }}' required></div><div class=field><label>New password (8+ chars)</label><input name=password type=password minlength=8 required></div><p><button>Update login</button></p></form><a class=btn href=/admin/backup>Download full backup</a></section></div><section class=panel><h2>Shop accounts</h2>{% for s in shops %}<div class=panel><b>{{s.name}}</b> · {{s.username}} · <b>{{'Active' if s.active else 'Deactivated'}}</b><div class=actions><a class=btn href=/admin/backup/{{s.id}}>Backup</a><form method=post action=/admin/toggle/{{s.id}}><button class='{{"danger" if s.active else ""}}'>{{'Deactivate' if s.active else 'Activate'}}</button></form></div><details><summary>Edit shop / password</summary><form method=post action=/admin/update/{{s.id}}><div class=formgrid><div class=field><label>Name</label><input name=name value='{{s.name}}' required></div><div class=field><label>Username</label><input name=username value='{{s.username}}' required></div><div class=field><label>New password (optional)</label><input type=password name=password minlength=8></div><div class=field><label>Phone</label><input name=phone value='{{s.phone}}'></div><div class=field><label>Address</label><input name=address value='{{s.address}}'></div></div><p><button>Save changes</button></p></form></details><details><summary>Restore shop backup</summary><form method=post enctype=multipart/form-data action=/admin/restore/{{s.id}} onsubmit='return confirm(\"Replace this shop data?\")'><input type=file name=backup accept=.json required><button class=danger>Restore</button></form></details></div>{% else %}<p>No shops yet.</p>{% endfor %}</section>"""
 return render_template_string(BASE+content+END,title="Admin",shops=shops)
@app.route("/admin/create",methods=["POST"])
@adminonly
def create_shop():
 name=val("name");u=val("username");pw=request.form.get("password","")
 if len(pw)<8:flash("Password must have at least 8 characters.","danger");return redirect(url_for("admin"))
 try:
  c=db();cur=c.execute("INSERT INTO shops(name,username,phone,address) VALUES(?,?,?,?)",(name,u,val("phone"),val("address")));sid=cur.lastrowid
  c.execute("INSERT INTO users(username,password_hash,role,shop_id) VALUES(?,?,?,?)",(u,generate_password_hash(pw),"shop",sid))
  for method in ("Cash","UPI","Card","Bank Transfer","Credit","Other"):c.execute("INSERT OR IGNORE INTO payment_methods(shop_id,name) VALUES(?,?)",(sid,method))
  for kind,names in [("expense","Rent,Electricity,Wages,Transport,Packaging,Repairs,Other".split(",")),("income","Commission,Scrap Sales,Interest Received,Other".split(","))]:
   for n in names:c.execute("INSERT OR IGNORE INTO categories(shop_id,kind,name) VALUES(?,?,?)",(sid,kind,n))
  c.commit();flash("Shop created.","success")
 except sqlite3.IntegrityError:db().rollback();flash("Username already exists.","danger")
 return redirect(url_for("admin"))
@app.route("/admin/update/<int:sid>",methods=["POST"])
@adminonly
def update_shop(sid):
 u=val("username");pw=request.form.get("password","")
 try:
  db().execute("UPDATE shops SET name=?,username=?,phone=?,address=? WHERE id=?",(val("name"),u,val("phone"),val("address"),sid))
  if pw and len(pw)<8:flash("Password must be at least 8 characters.","danger");return redirect(url_for("admin"))
  if pw:db().execute("UPDATE users SET username=?,password_hash=? WHERE shop_id=? AND role='shop'",(u,generate_password_hash(pw),sid))
  else:db().execute("UPDATE users SET username=? WHERE shop_id=? AND role='shop'",(u,sid))
  db().commit();flash("Shop updated.","success")
 except sqlite3.IntegrityError:db().rollback();flash("Username already exists.","danger")
 return redirect(url_for("admin"))
@app.route("/admin/password",methods=["POST"])
@adminonly
def admin_password():
 u=val("username");pw=request.form.get("password","")
 if not u or len(pw)<8:flash("Username required and password must be 8+ characters.","danger");return redirect(url_for("admin"))
 try:db().execute("UPDATE users SET username=?,password_hash=? WHERE id=?",(u,generate_password_hash(pw),session["user_id"]));db().commit();session["username"]=u;flash("Admin login updated.","success")
 except sqlite3.IntegrityError:db().rollback();flash("Username exists.","danger")
 return redirect(url_for("admin"))
@app.route("/admin/toggle/<int:sid>",methods=["POST"])
@adminonly
def toggle(sid):
 s=db().execute("SELECT active FROM shops WHERE id=?",(sid,)).fetchone()
 if s:db().execute("UPDATE shops SET active=? WHERE id=?",(1-s["active"],sid));db().execute("UPDATE users SET active=? WHERE shop_id=?",(1-s["active"],sid));db().commit()
 return redirect(url_for("admin"))
def shopbackup(sid):
 c=db();out={"format":"easykada-shop-v1","shop_id":sid,"exported_at":now(),"data":{}}
 for t in ["shops","products","customers","suppliers","sales","sale_items","purchases","purchase_payments","expenses","incomes","categories","payment_methods","sales_returns","sales_return_items","purchase_returns","purchase_return_items"]:
  if t=="shops":rs=c.execute("SELECT * FROM shops WHERE id=?",(sid,)).fetchall()
  elif t=="sale_items":rs=c.execute("SELECT i.* FROM sale_items i JOIN sales s ON s.id=i.sale_id WHERE s.shop_id=?",(sid,)).fetchall()
  elif t in ("sales_return_items","purchase_return_items"):rs=c.execute(f"SELECT i.* FROM {t} i JOIN {'sales_returns' if t=='sales_return_items' else 'purchase_returns'} r ON r.id=i.return_id WHERE r.shop_id=?",(sid,)).fetchall()
  else:rs=c.execute(f"SELECT * FROM {t} WHERE shop_id=?",(sid,)).fetchall()
  out["data"][t]=[dict(r) for r in rs]
 return out
@app.route("/admin/backup")
@adminonly
def backup_all():
 c=db();data={"format":"easykada-full-v1","users":[dict(r) for r in c.execute("SELECT * FROM users")],"shops":[dict(r) for r in c.execute("SELECT * FROM shops")],"shops_data":{}}
 for s in data["shops"]:data["shops_data"][str(s["id"])]=shopbackup(s["id"])["data"]
 return send_file(io.BytesIO(json.dumps(data,indent=2).encode()),mimetype="application/json",as_attachment=True,download_name="easykada-full-backup.json")
@app.route("/admin/backup/<int:sid>")
@adminonly
def backup_one(sid):
 return send_file(io.BytesIO(json.dumps(shopbackup(sid),indent=2).encode()),mimetype="application/json",as_attachment=True,download_name=f"easykada-shop-{sid}-backup.json")
@app.route("/admin/restore/<int:sid>",methods=["POST"])
@adminonly
def restore_one(sid):
 f=request.files.get("backup")
 try:data=json.load(f.stream)["data"]
 except:flash("Invalid shop backup JSON.","danger");return redirect(url_for("admin"))
 c=db()
 try:
  c.execute("BEGIN")
  for t in ["sale_items","sales_return_items","purchase_return_items","sales_returns","purchase_returns","sales","purchase_payments","purchases","expenses","incomes","products","customers","suppliers","categories","payment_methods"]:
   if t=="sale_items":c.execute("DELETE FROM sale_items WHERE sale_id IN (SELECT id FROM sales WHERE shop_id=?)",(sid,))
   elif t=="sales_return_items":c.execute("DELETE FROM sales_return_items WHERE return_id IN (SELECT id FROM sales_returns WHERE shop_id=?)",(sid,))
   elif t=="purchase_return_items":c.execute("DELETE FROM purchase_return_items WHERE return_id IN (SELECT id FROM purchase_returns WHERE shop_id=?)",(sid,))
   else:c.execute(f"DELETE FROM {t} WHERE shop_id=?",(sid,))
  for t in ["products","customers","suppliers","sales","sale_items","purchases","purchase_payments","expenses","incomes","categories","payment_methods","sales_returns","sales_return_items","purchase_returns","purchase_return_items"]:
   for row in data.get(t,[]):
    row=dict(row);cols={x["name"] for x in c.execute(f"PRAGMA table_info({t})")}
    row={k:v for k,v in row.items() if k in cols}
    if "shop_id" in cols:row["shop_id"]=sid
    if t in ("sales_return_items","purchase_return_items") and "return_id" in row:
     old_parent=row["return_id"]; parent_table="sales_returns" if t=="sales_return_items" else "purchase_returns"
     # Restored parents retain their ids in a fresh shop restore.
    c.execute(f"INSERT INTO {t} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",list(row.values()))
  c.commit();flash("Shop backup restored.","success")
 except Exception as e:c.rollback();flash("Restore failed; rolled back: "+str(e),"danger")
 return redirect(url_for("admin"))
@app.route("/shop")
@shoponly
def dashboard():
 sid=shopid();d=today();c=db();s=c.execute("SELECT * FROM sales WHERE shop_id=? AND sale_date=?",(sid,d)).fetchall()
 rev=sum(x["total"] or 0 for x in s);received=sum(x["received"] or 0 for x in s);cost=sum(x["cost_total"] or 0 for x in s)
 exp=c.execute("SELECT COALESCE(SUM(amount),0) n FROM expenses WHERE shop_id=? AND entry_date=?",(sid,d)).fetchone()["n"]
 low=rows("SELECT * FROM products WHERE shop_id=? AND active=1 AND stock<=low_stock ORDER BY stock LIMIT 12")
 near=c.execute("SELECT p.product_name,p.batch_no,p.expiry_date,p.qty,p.product_id,pr.stock,pr.unit FROM purchases p LEFT JOIN products pr ON pr.id=p.product_id WHERE p.shop_id=? AND p.expiry_date<>'' AND date(p.expiry_date)>=date('now') AND date(p.expiry_date)<=date('now','+1 month') AND COALESCE(pr.stock,0)>0 ORDER BY p.expiry_date LIMIT 20",(sid,)).fetchall()
 content="""<h1>Shop Dashboard</h1><div class=grid>{% for label,value in [('Today sales',rev),('Received',received),('Estimated gross profit',rev-cost),('Today expenses',exp)] %}<div class=metric><span class=muted>{{label}}</span><b>{{value|money}}</b></div>{% endfor %}</div><section class=panel><h2>Low stock alerts</h2>{% for p in low %}<p>{{p.name}} <b style='float:right'>{{p.stock}} {{p.unit}}</b></p>{% else %}<p>No low stock items.</p>{% endfor %}</section><section class=panel><h2>Near expiry — next 30 days</h2>{% for b in near %}<p><b>{{b.product_name}}</b> · Batch {{b.batch_no or 'Not entered'}} · Expiry {{b.expiry_date}} <span style='float:right'>Stock: {{b.stock}} {{b.unit}}</span></p>{% else %}<p>No batches expiring within one month.</p>{% endfor %}</section>"""
 return render_template_string(BASE+content+END,rev=rev,received=received,cost=cost,exp=exp,low=low,near=near)
@app.route("/products",methods=["GET","POST"])
@shoponly
def products():
 if request.method=="POST":
  pid=val("id");name=val("name");price=num("price");stock=num("stock")
  if not name or price<0 or stock<0:flash("Enter valid product details.","danger");return redirect(url_for("products"))
  vals=(name,val("code"),val("category"),val("unit","pcs"),price,num("cost"),stock,num("low_stock",5),val("barcode"))
  if pid:db().execute("UPDATE products SET name=?,code=?,category=?,unit=?,price=?,cost=?,stock=?,low_stock=?,barcode=? WHERE id=? AND shop_id=?",(*vals,int(pid),shopid()))
  else:db().execute("INSERT INTO products(shop_id,name,code,category,unit,price,cost,stock,low_stock,barcode) VALUES(?,?,?,?,?,?,?,?,?,?)",(shopid(),*vals))
  db().commit();flash("Product saved.","success");return redirect(url_for("products"))
 ps=[dict(r) for r in rows("SELECT * FROM products WHERE shop_id=? ORDER BY name")]
 content="""<h1>Products & Stock</h1><section class=panel><h2>Add / edit product</h2><form method=post><input type=hidden name=id id=pid><div class=formgrid>{% for name,label,typ in [('name','Product name','text'),('code','SKU / code','text'),('category','Category','text'),('unit','Unit','text'),('price','Selling price','number'),('cost','Cost price','number'),('stock','Opening stock','number'),('low_stock','Low stock at','number'),('barcode','Barcode','text')] %}<div class=field><label>{{label}}</label><input name={{name}} id=p_{{name}} type={{typ}} {% if typ=='number' %}step=0.001 min=0{% endif %} {% if name=='name' or name=='price' %}required{% endif %}></div>{% endfor %}</div><p><button>Save product</button> <button type=button class=btn2 onclick='document.querySelector(\"form\").reset();document.getElementById(\"pid\").value=\"\"'>Clear</button></p></form></section><section class=panel><input placeholder='Search products' oninput='filterTable(\"pt\",this.value)'><div class=tablewrap><table id=pt><thead><tr><th>Name</th><th>Code</th><th>Price/unit</th><th>Cost</th><th>Stock</th><th>Actions</th></tr></thead><tbody>{% for p in ps %}<tr><td>{{p.name}}</td><td>{{p.code}}</td><td>{{p.price|money}} / {{p.unit}}</td><td>{{p.cost|money}}</td><td>{{p.stock}}</td><td><button onclick='editP({{p|tojson}})'>Edit</button><form method=post action=/products/delete/{{p.id}} style='display:inline' onsubmit='return confirm(\"Delete/archive product?\")'><button class=danger>Delete</button></form></td></tr>{% endfor %}</tbody></table></div></section><script>function editP(p){document.getElementById('pid').value=p.id;for(let k of ['name','code','category','unit','price','cost','stock','low_stock','barcode'])document.getElementById('p_'+k).value=p[k]??'';window.scrollTo(0,0)}</script>"""
 return render_template_string(BASE+content+END,ps=ps)
@app.route("/products/delete/<int:pid>",methods=["POST"])
@shoponly
def product_delete(pid):
 p=getp(pid)
 if not p:abort(404)
 if db().execute("SELECT 1 FROM sale_items WHERE product_id=?",(pid,)).fetchone() or db().execute("SELECT 1 FROM purchases WHERE product_id=?",(pid,)).fetchone():db().execute("UPDATE products SET active=0 WHERE id=?",(pid,));flash("Product archived because it has history.","warning")
 else:db().execute("DELETE FROM products WHERE id=?",(pid,));flash("Product deleted.","success")
 db().commit();return redirect(url_for("products"))
@app.route("/pos",methods=["GET","POST"])
@shoponly
def pos():
 if request.method=="POST":
  c=db();sid=val("sale_id");old=c.execute("SELECT * FROM sales WHERE id=? AND shop_id=?",(int(sid),shopid())).fetchone() if sid else None
  try:
   if old:
    for it in c.execute("SELECT * FROM sale_items WHERE sale_id=?",(old["id"],)).fetchall():
     if it["product_id"]:c.execute("UPDATE products SET stock=stock+? WHERE id=?",(it["qty"],it["product_id"]))
    c.execute("DELETE FROM sale_items WHERE sale_id=?",(old["id"],))
   pids=request.form.getlist("product_id[]");qtys=request.form.getlist("qty[]");lines=[]
   for pid,q in zip(pids,qtys):
    p=getp(int(pid));q=float(q)
    if not p or not p["active"] or q<=0:continue
    if q>p["stock"]:raise ValueError("Insufficient stock for "+p["name"])
    lines.append((p,q))
   if not lines:raise ValueError("Add at least one item.")
   subtotal=sum(p["price"]*q for p,q in lines);discount=num("discount")
   if discount<0 or discount>subtotal:raise ValueError("Discount cannot exceed subtotal.")
   total=subtotal-discount;method=val("payment_method","Cash");received=0 if method=="Credit" else (total if val("received")=="" else num("received"));received=max(0,min(received,total));due=total-received
   custid=val("customer_id");cust=c.execute("SELECT * FROM customers WHERE id=? AND shop_id=?",(int(custid),shopid())).fetchone() if custid else None
   cost=sum(p["cost"]*q for p,q in lines)
   if old:
    invoice=old["invoice"];saleid=old["id"];c.execute("UPDATE sales SET customer_id=?,customer_name=?,subtotal=?,discount=?,total=?,payment_method=?,received=?,due=?,cost_total=? WHERE id=?",(cust["id"] if cust else None,cust["name"] if cust else "Walk-in",subtotal,discount,total,method,received,due,cost,saleid))
   else:
    invoice="INV-"+datetime.now().strftime("%y%m%d-%H%M%S");cur=c.execute("INSERT INTO sales(shop_id,invoice,sale_date,sale_time,customer_id,customer_name,subtotal,discount,total,payment_method,received,due,cost_total) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(shopid(),invoice,today(),datetime.now().strftime("%H:%M"),cust["id"] if cust else None,cust["name"] if cust else "Walk-in",subtotal,discount,total,method,received,due,cost));saleid=cur.lastrowid
   for p,q in lines:c.execute("INSERT INTO sale_items(sale_id,product_id,product_name,unit,qty,price,cost,line_total) VALUES(?,?,?,?,?,?,?,?)",(saleid,p["id"],p["name"],p["unit"],q,p["price"],p["cost"],q*p["price"]));c.execute("UPDATE products SET stock=stock-? WHERE id=?",(q,p["id"]))
   c.commit();flash("Bill saved: "+invoice,"success");return redirect(url_for("history"))
  except Exception as e:c.rollback();flash(str(e),"danger");return redirect(url_for("pos"))
 editid=request.args.get("edit_id",type=int);edit=db().execute("SELECT * FROM sales WHERE id=? AND shop_id=?",(editid,shopid())).fetchone() if editid else None
 items=db().execute("SELECT * FROM sale_items WHERE sale_id=?",(editid,)).fetchall() if edit else []
 ps=rows("SELECT * FROM products WHERE shop_id=? AND active=1 AND stock>0 ORDER BY name");cs=rows("SELECT * FROM customers WHERE shop_id=? ORDER BY name");methods=rows("SELECT name FROM payment_methods WHERE shop_id=? AND active=1 ORDER BY name")
 content="""<h1>{{'Edit '+edit.invoice if edit else 'New Sale / POS'}}</h1><form method=post id=sf><input type=hidden name=sale_id value='{{edit.id if edit else \"\"}}'><div class=grid><section class=panel><label>Search products</label><input id=search placeholder='Name or code' oninput='searchP()'><div class=products id=products>{% for p in ps %}<button type=button class=tile data-pid={{p.id}} data-name='{{p.name}}' data-price={{p.price}} data-cost={{p.cost}} data-stock={{p.stock}} data-unit={{p.unit}} data-search='{{(p.name+\" \"+p.code)|lower}}' onclick='add(this)'><b>{{p.name}}</b><span>{{p.price|money}} / {{p.unit}}</span><small>Stock: {{p.stock}}</small></button>{% endfor %}</div></section><section class=panel><h2>Bill items</h2><label>Customer</label><select name=customer_id><option value=''>Walk-in</option>{% for c in cs %}<option value={{c.id}} {% if edit and edit.customer_id==c.id %}selected{% endif %}>{{c.name}}</option>{% endfor %}</select><div class=tablewrap><table><thead><tr><th>Item</th><th>Qty</th><th>Price</th><th></th></tr></thead><tbody id=cart></tbody></table></div><div class=formgrid><div class=field><label>Discount ₹</label><input id=discount name=discount type=number min=0 step=.01 value='{{edit.discount if edit else 0}}' oninput=render()></div><div class=field><label>Payment method</label><select name=payment_method>{% for m in methods %}<option {% if edit and edit.payment_method==m.name %}selected{% endif %}>{{m.name}}</option>{% endfor %}</select></div><div class=field><label>Amount received ₹</label><input name=received type=number min=0 step=.01 value='{{edit.received if edit else \"\"}}' placeholder='Blank = full'></div></div><p>Total <b class=total id=total>₹0.00</b></p><button>{{'Update existing bill' if edit else 'Complete sale'}}</button>{% if edit %}<a class=btn href=/history>Cancel edit</a>{% endif %}</section></div></form><script>
let cart=new Map();function add(b,q=1){let id=b.dataset.pid,stock=+b.dataset.stock;if(!cart.has(id))cart.set(id,{id,name:b.dataset.name,price:+b.dataset.price,cost:+b.dataset.cost,stock,unit:b.dataset.unit,qty:0});let x=cart.get(id);if(x.qty+q>x.stock){alert('Not enough stock');return}x.qty+=q;render()}
function render(){let body=document.getElementById('cart');body.innerHTML='';let sub=0;cart.forEach(x=>{sub+=x.price*x.qty;let tr=document.createElement('tr');tr.innerHTML='<td>'+x.name+'<input type=hidden name=\"product_id[]\" value=\"'+x.id+'\"></td><td><input type=number min=.001 step=.001 value=\"'+x.qty+'\" style=\"width:75px\" onchange=\"qty(\\''+x.id+'\\',this.value)\"><input type=hidden name=\"qty[]\" value=\"'+x.qty+'\"></td><td>'+x.price.toFixed(2)+'</td><td><button type=button class=danger onclick=\"del(\\''+x.id+'\\')\">×</button></td>';body.appendChild(tr)});document.getElementById('total').textContent='₹'+Math.max(0,sub-(+document.getElementById('discount').value||0)).toFixed(2)}
function qty(id,v){let x=cart.get(id),n=+v;if(n<=0||n>x.stock){alert('Check quantity/stock');render();return}x.qty=n;render()}function del(id){cart.delete(id);render()}function searchP(){let q=document.getElementById('search').value.toLowerCase();document.querySelectorAll('[data-pid]').forEach(b=>b.hidden=!b.dataset.search.includes(q))}
{% if edit %}{% for it in items %}{% for p in ps if p.id==it.product_id %}add(document.querySelector('[data-pid=\"{{p.id}}\"]'),{{it.qty}});{% endfor %}{% endfor %}{% endif %}
document.getElementById('sf').addEventListener('submit',e=>{if(!cart.size){e.preventDefault();alert('Add items first')}});render();</script>"""
 return render_template_string(BASE+content+END,ps=ps,cs=cs,edit=edit,items=items,methods=methods)
@app.route("/history")
@shoponly
def history():
 frm=request.args.get("from","");to=request.args.get("to","");q=request.args.get("q","")
 sql="SELECT * FROM sales WHERE shop_id=?";args=[]
 if frm:sql+=" AND sale_date>=?";args.append(frm)
 if to:sql+=" AND sale_date<=?";args.append(to)
 if q:sql+=" AND (invoice LIKE ? OR customer_name LIKE ?)";args.extend(["%"+q+"%","%"+q+"%"])
 sales=db().execute(sql+" ORDER BY sale_date DESC,id DESC",(shopid(),*args)).fetchall()
 content="""<h1>Sales History & Bills</h1><section class=panel><form class=formgrid><div class=field><label>From</label><input type=date name=from value='{{frm}}'></div><div class=field><label>To</label><input type=date name=to value='{{to}}'></div><div class=field><label>Invoice/customer</label><input name=q value='{{q}}'></div><div><button>Search</button></div></form><div class=tablewrap><table><thead><tr><th>Date</th><th>Invoice</th><th>Customer</th><th>Total</th><th>Paid</th><th>Due</th><th>Actions</th></tr></thead><tbody>{% for s in sales %}<tr><td>{{s.sale_date}}</td><td>{{s.invoice}}</td><td>{{s.customer_name}}</td><td>{{s.total|money}}</td><td>{{s.received|money}}</td><td>{{s.due|money}}</td><td class=actions><a class=btn href='/pos?edit_id={{s.id}}'>Edit</a><a class=btn target=_blank href='/sale/{{s.id}}/whatsapp'>WhatsApp</a><form method=post action='/sale/{{s.id}}/delete' onsubmit='return confirm(\"Delete bill and restore stock?\")'><button class=danger>Delete</button></form><a class='btn btn2' target=_blank href='/sale/{{s.id}}/print'>Print</a></td></tr>{% else %}<tr><td colspan=7>No bills found.</td></tr>{% endfor %}</tbody></table></div></section>"""
 return render_template_string(BASE+content+END,sales=sales,frm=frm,to=to,q=q)
@app.route("/sale/<int:sid>/delete",methods=["POST"])
@shoponly
def sale_delete(sid):
 c=db();s=c.execute("SELECT * FROM sales WHERE id=? AND shop_id=?",(sid,shopid())).fetchone()
 if not s:abort(404)
 for it in c.execute("SELECT * FROM sale_items WHERE sale_id=?",(sid,)).fetchall():
  if it["product_id"]:c.execute("UPDATE products SET stock=stock+? WHERE id=? AND shop_id=?",(it["qty"],it["product_id"],shopid()))
 c.execute("DELETE FROM sale_items WHERE sale_id=?",(sid,));c.execute("DELETE FROM sales WHERE id=?",(sid,));c.commit();flash("Bill deleted; stock restored and reports updated.","success");return redirect(url_for("history"))
@app.route("/sale/<int:sid>/whatsapp")
@shoponly
def sale_wa(sid):
 s=db().execute("SELECT * FROM sales WHERE id=? AND shop_id=?",(sid,shopid())).fetchone()
 if not s:abort(404)
 items=db().execute("SELECT * FROM sale_items WHERE sale_id=?",(sid,)).fetchall()
 c=db().execute("SELECT * FROM customers WHERE id=? AND shop_id=?",(s["customer_id"],shopid())).fetchone() if s["customer_id"] else None
 phone=(c["whatsapp"] or c["phone"]) if c else ""
 msg=["━━━━━━━━━━━━━━━━━━━━",g.shop["name"].upper(),g.shop["address"] or "",("Phone: "+g.shop["phone"]) if g.shop["phone"] else "","🧾 SALES INVOICE", "Invoice No: "+s["invoice"],"Date: "+str(s["sale_date"])+"  Time: "+str(s["sale_time"] or ""),"Customer: "+str(s["customer_name"] or "Walk-in"),"━━━━━━━━━━━━━━━━━━━━","ITEM                 QTY × RATE       AMOUNT"]
 for i in items:msg.append(f'{i["product_name"]} | {i["qty"]:g} {i["unit"]} × ₹{i["price"]:.2f} = ₹{i["line_total"]:.2f}')
 msg += ["━━━━━━━━━━━━━━━━━━━━",f'Subtotal: ₹{s["subtotal"]:.2f}',f'Discount: ₹{s["discount"]:.2f}',f'GRAND TOTAL: ₹{s["total"]:.2f}',f'Payment: {s["payment_method"] or "—"}',f'Amount received: ₹{s["received"]:.2f}',f'BALANCE DUE: ₹{s["due"]:.2f}',"━━━━━━━━━━━━━━━━━━━━","Thank you for shopping with us!","Please contact us if you have any questions."]
 import urllib.parse
 return redirect("https://wa.me/"+("".join(ch for ch in phone if ch.isdigit()) if phone else "")+"?text="+urllib.parse.quote("\\n".join(msg)))
@app.route("/sale/<int:sid>/print")
@shoponly
def sale_print(sid):
 s=db().execute("SELECT * FROM sales WHERE id=? AND shop_id=?",(sid,shopid())).fetchone()
 if not s:abort(404)
 its=db().execute("SELECT * FROM sale_items WHERE sale_id=?",(sid,)).fetchall()
 content="""<div class=panel><h2 style='text-align:center'>{{g.shop.name}}</h2><p style='text-align:center'>Invoice {{s.invoice}} · {{s.sale_date}}</p><table><thead><tr><th>Item</th><th>Qty</th><th>Price</th><th>Total</th></tr></thead><tbody>{% for i in its %}<tr><td>{{i.product_name}}</td><td>{{i.qty}} {{i.unit}}</td><td>{{i.price|money}}</td><td>{{i.line_total|money}}</td></tr>{% endfor %}</tbody></table><h2 style='text-align:right'>Total {{s.total|money}}</h2><p>Paid {{s.received|money}} · Due {{s.due|money}}</p><button onclick='print()'>Print</button></div>"""
 return render_template_string(BASE+content+END,s=s,its=its)
@app.route("/purchases",methods=["GET","POST"])
@shoponly
def purchases():
 c=db()
 if request.method=="POST":
  try:
   sup=c.execute("SELECT * FROM suppliers WHERE id=? AND shop_id=?",(int(val("supplier_id") or 0),shopid())).fetchone()
   pids=request.form.getlist("product_id[]"); qtys=request.form.getlist("qty[]"); costs=request.form.getlist("unit_cost[]")
   batches=request.form.getlist("batch_no[]"); expiries=request.form.getlist("expiry_date[]")
   lines=[]
   for pid,q,co,batch,expiry in zip(pids,qtys,costs,batches,expiries):
    if not pid:continue
    prod=getp(int(pid));qty=float(q or 0);cost=float(co or 0)
    if not prod or qty<=0 or cost<0:raise ValueError("Check each product, quantity and unit cost.")
    if expiry and expiry<val("purchase_date",today()):raise ValueError("Expiry date cannot be earlier than purchase date.")
    lines.append((prod,qty,cost,batch.strip(),expiry.strip()))
   if not lines:raise ValueError("Add at least one item to the supplier invoice.")
   pdate=val("purchase_date",today());invoice=val("invoice_no") or ("PUR-"+datetime.now().strftime("%y%m%d-%H%M%S"))
   grand=sum(q*co for _,q,co,_,_ in lines);paid=num("paid")
   if paid<0 or paid>grand:raise ValueError("Paid amount must be between zero and invoice total.")
   method=val("method","Cash");remaining_paid=paid
   for prod,qty,cost,batch,expiry in lines:
    total=qty*cost;line_paid=min(total,remaining_paid);remaining_paid-=line_paid
    cur=c.execute("INSERT INTO purchases(shop_id,purchase_date,supplier_id,supplier_name,product_id,product_name,qty,unit_cost,total,paid,due,notes,invoice_no,batch_no,expiry_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(shopid(),pdate,sup["id"] if sup else None,sup["name"] if sup else "",prod["id"],prod["name"],qty,cost,total,line_paid,total-line_paid,val("notes"),invoice,batch,expiry))
    purchase_id=cur.lastrowid
    c.execute("UPDATE products SET stock=stock+?,cost=? WHERE id=? AND shop_id=?",(qty,cost,prod["id"],shopid()))
    if line_paid>0:c.execute("INSERT INTO purchase_payments(shop_id,purchase_id,payment_date,amount,method) VALUES(?,?,?,?,?)",(shopid(),purchase_id,pdate,line_paid,method))
   c.commit();flash(f"Purchase invoice {invoice} posted ({len(lines)} items). Stock, batch and expiry records updated.","success");return redirect(url_for("purchases"))
  except Exception as e:c.rollback();flash(str(e),"danger");return redirect(url_for("purchases"))
 ps=rows("SELECT * FROM products WHERE shop_id=? AND active=1 ORDER BY name");ss=rows("SELECT * FROM suppliers WHERE shop_id=? ORDER BY name")
 methods=rows("SELECT name FROM payment_methods WHERE shop_id=? AND active=1 ORDER BY name")
 frm=request.args.get("from","");to=request.args.get("to","")
 purchases=rows("SELECT * FROM purchases WHERE shop_id=? ORDER BY purchase_date DESC,invoice_no DESC,id DESC")
 content="""<h1>Purchase Invoice Posting</h1><section class=panel><p class=muted>Enter one supplier invoice with multiple item lines. Each line stores its own batch number and expiry date.</p><form method=post id=pf><div class=formgrid><div class=field><label>Invoice date</label><input type=date name=purchase_date value='{{today()}}' required></div><div class=field><label>Supplier invoice no.</label><input name=invoice_no placeholder='Supplier invoice reference'></div><div class=field><label>Supplier</label><select name=supplier_id><option value=''>Select supplier</option>{% for s in ss %}<option value={{s.id}}>{{s.name}}</option>{% endfor %}</select></div><div class=field><label>Paid now ₹</label><input name=paid type=number min=0 step=.01 value=0 required></div><div class=field><label>Payment method</label><select name=method>{% for m in methods %}<option>{{m.name}}</option>{% endfor %}</select></div><div class=field><label>Notes</label><input name=notes></div></div><h3>Invoice items</h3><div class=tablewrap><table><thead><tr><th>Product</th><th>Qty</th><th>Unit cost ₹</th><th>Batch no.</th><th>Expiry date</th><th></th></tr></thead><tbody id=plines></tbody></table></div><p><button type=button onclick=addLine()>+ Add item</button></p><p>Invoice total: <b class=total id=ptotal>₹0.00</b></p><button>Post purchase invoice</button></form></section><section class=panel><h2>Purchase invoice history</h2><form class=formgrid><div class=field><label>From date</label><input type=date name=from value='{{frm}}'></div><div class=field><label>To date</label><input type=date name=to value='{{to}}'></div><div><button>Filter</button></div></form><div class=tablewrap><table><thead><tr><th>Date</th><th>Invoice</th><th>Batch</th><th>Expiry</th><th>Product / Qty</th><th>Supplier</th><th>Total</th><th>Paid</th><th>Due</th><th>Actions</th></tr></thead><tbody>{% for p in purchases if (not frm or p.purchase_date>=frm) and (not to or p.purchase_date<=to) %}<tr><td>{{p.purchase_date}}</td><td>{{p.invoice_no}}</td><td>{{p.batch_no or '—'}}</td><td>{{p.expiry_date or '—'}}</td><td>{{p.product_name}} ({{p.qty}})</td><td>{{p.supplier_name}}</td><td>{{p.total|money}}</td><td>{{p.paid|money}}</td><td>{{p.due|money}}</td><td class=actions>{% if p.due>0 %}<form method=post action=/purchase/{{p.id}}/pay><input name=amount type=number min=.01 max={{p.due}} step=.01 placeholder='Pay ₹' required style='width:85px'><select name=method style='width:110px'>{% for m in methods %}<option>{{m.name}}</option>{% endfor %}</select><button>Pay</button></form>{% endif %}<form method=post action=/purchase/{{p.id}}/delete onsubmit='return confirm("Delete this purchase line and adjust stock?")'><button class=danger>Delete line</button></form></td></tr>{% endfor %}</tbody></table></div></section><script>
const productOptions=`{% for p in ps %}<option value="{{p.id}}">{{p.name|e}} (stock {{p.stock}})</option>{% endfor %}`;
function addLine(){let tr=document.createElement('tr');tr.innerHTML='<td><select name="product_id[]" required>'+productOptions+'</select></td><td><input name="qty[]" type=number min=.001 step=.001 value=1 required oninput=calcPurchase()></td><td><input name="unit_cost[]" type=number min=0 step=.01 value=0 required oninput=calcPurchase()></td><td><input name="batch_no[]" placeholder="Batch no."></td><td><input name="expiry_date[]" type=date></td><td><button type=button class=danger onclick="this.closest('tr').remove();calcPurchase()">×</button></td>';document.getElementById('plines').appendChild(tr);calcPurchase()}
function calcPurchase(){let n=0;document.querySelectorAll('#plines tr').forEach(r=>n+=(+r.querySelector('[name="qty[]"]').value||0)*(+r.querySelector('[name="unit_cost[]"]').value||0));document.getElementById('ptotal').textContent='₹'+n.toFixed(2)}addLine();
</script>"""
 return render_template_string(BASE+content+END,ps=ps,ss=ss,purchases=purchases,frm=frm,to=to,methods=methods)
@app.route("/purchase/<int:pid>/pay",methods=["POST"])
@shoponly
def purchase_pay(pid):
 c=db();p=c.execute("SELECT * FROM purchases WHERE id=? AND shop_id=?",(pid,shopid())).fetchone();a=num("amount")
 if not p or a<=0 or a>p["due"]+.001:flash("Payment amount exceeds balance or is invalid.","danger")
 else:c.execute("INSERT INTO purchase_payments(shop_id,purchase_id,payment_date,amount,method) VALUES(?,?,?,?,?)",(shopid(),pid,today(),a,val("method","Cash")));c.execute("UPDATE purchases SET paid=paid+?,due=MAX(0,due-?) WHERE id=?",(a,a,pid));c.commit();flash("Supplier payment saved.","success")
 return redirect(url_for("purchases"))
@app.route("/purchase/<int:pid>/delete",methods=["POST"])
@shoponly
def purchase_delete(pid):
 c=db();p=c.execute("SELECT * FROM purchases WHERE id=? AND shop_id=?",(pid,shopid())).fetchone()
 if p:
  if p["product_id"]:c.execute("UPDATE products SET stock=MAX(0,stock-?) WHERE id=?",(p["qty"],p["product_id"]))
  c.execute("DELETE FROM purchase_payments WHERE purchase_id=?",(pid,));c.execute("DELETE FROM purchases WHERE id=?",(pid,));c.commit();flash("Purchase deleted; stock adjusted. Review inventory if goods were already sold.","warning")
 return redirect(url_for("purchases"))
@app.route("/entries",methods=["GET","POST"])
@shoponly
def entries():
 c=db()
 if request.method=="POST":
  kind=val("kind","expense");eid=val("id");cat=val("category");amt=num("amount")
  if kind not in ("expense","income") or amt<=0 or not cat:flash("Enter a category and positive amount.","danger");return redirect(url_for("entries"))
  t="expenses" if kind=="expense" else "incomes"
  if eid:c.execute(f"UPDATE {t} SET entry_date=?,category=?,amount=?,payment_method=?,notes=? WHERE id=? AND shop_id=?",(val("entry_date",today()),cat,amt,val("method","Cash"),val("notes"),int(eid),shopid()))
  else:c.execute(f"INSERT INTO {t}(shop_id,entry_date,category,amount,payment_method,notes) VALUES(?,?,?,?,?,?)",(shopid(),val("entry_date",today()),cat,amt,val("method","Cash"),val("notes")))
  c.commit();flash("Entry saved.","success");return redirect(url_for("entries"))
 exp=rows("SELECT * FROM expenses WHERE shop_id=? ORDER BY entry_date DESC,id DESC");inc=rows("SELECT * FROM incomes WHERE shop_id=? ORDER BY entry_date DESC,id DESC")
 cats=rows("SELECT * FROM categories WHERE shop_id=? ORDER BY kind,name");methods=rows("SELECT name FROM payment_methods WHERE shop_id=? AND active=1 ORDER BY name")
 editkind=request.args.get("kind","expense");eid=request.args.get("edit_id",type=int);edit=None
 if eid:edit=c.execute("SELECT * FROM "+("expenses" if editkind=="expense" else "incomes")+" WHERE id=? AND shop_id=?",(eid,shopid())).fetchone()
 content="""<h1>Other Income / Expenses</h1><section class=panel><h2>{{'Edit entry' if edit else 'Add entry'}}</h2><form method=post><input type=hidden name=id value='{{edit.id if edit else \"\"}}'><div class=formgrid><div class=field><label>Type</label><select name=kind id=kind onchange=sync()><option value=expense {% if editkind=='expense' %}selected{% endif %}>Expense</option><option value=income {% if editkind=='income' %}selected{% endif %}>Other income</option></select></div><div class=field><label>Date</label><input type=date name=entry_date value='{{edit.entry_date if edit else today()}}'></div><div class=field><label>Category</label><select name=category id=cat>{% for c in cats %}<option data-kind={{c.kind}} {% if edit and edit.category==c.name and editkind==c.kind %}selected{% endif %}>{{c.name}}</option>{% endfor %}</select></div><div class=field><label>Amount ₹</label><input name=amount type=number min=.01 step=.01 value='{{edit.amount if edit else \"\"}}' required></div><div class=field><label>Payment</label><select name=method>{% for m in methods %}<option>{{m.name}}</option>{% endfor %}</select></div><div class=field><label>Notes</label><input name=notes value='{{edit.notes if edit else \"\"}}'></div></div><p><button>Save</button></p></form></section><div class=grid><section class=panel><h2>Add expense category</h2><form method=post action=/category><input type=hidden name=kind value=expense><input name=name placeholder='Category name' required><button>Add</button></form></section><section class=panel><h2>Add income category</h2><form method=post action=/category><input type=hidden name=kind value=income><input name=name placeholder='Category name' required><button>Add</button></form></section></div><div class=grid><section class=panel><h2>Expenses</h2><div class=tablewrap><table><thead><tr><th>Date</th><th>Category</th><th>Amount</th><th>Actions</th></tr></thead><tbody>{% for e in exp %}<tr><td>{{e.entry_date}}</td><td>{{e.category}}</td><td>{{e.amount|money}}</td><td class=actions><a class=btn href='/entries?kind=expense&edit_id={{e.id}}'>Edit</a><form method=post action='/entries/delete/expense/{{e.id}}' onsubmit='return confirm(\"Delete?\")'><button class=danger>Delete</button></form></td></tr>{% endfor %}</tbody></table></div></section><section class=panel><h2>Other income</h2><div class=tablewrap><table><thead><tr><th>Date</th><th>Category</th><th>Amount</th><th>Actions</th></tr></thead><tbody>{% for e in inc %}<tr><td>{{e.entry_date}}</td><td>{{e.category}}</td><td>{{e.amount|money}}</td><td class=actions><a class=btn href='/entries?kind=income&edit_id={{e.id}}'>Edit</a><form method=post action='/entries/delete/income/{{e.id}}' onsubmit='return confirm(\"Delete?\")'><button class=danger>Delete</button></form></td></tr>{% endfor %}</tbody></table></div></section></div><script>function sync(){let k=document.getElementById('kind').value;let opts=[...document.querySelectorAll('#cat option')];opts.forEach(o=>o.hidden=o.dataset.kind!==k);let x=opts.find(o=>o.dataset.kind===k);if(x)x.selected=true}sync()</script>"""
 return render_template_string(BASE+content+END,exp=exp,inc=inc,cats=cats,edit=edit,editkind=editkind,methods=methods)
@app.route("/category",methods=["POST"])
@shoponly
def category():
 k=val("kind");n=val("name")
 if k not in ("expense","income") or not n:flash("Invalid category.","danger")
 else:
  try:db().execute("INSERT INTO categories(shop_id,kind,name) VALUES(?,?,?)",(shopid(),k,n));db().commit();flash("Category added.","success")
  except sqlite3.IntegrityError:flash("Category exists.","warning")
 return redirect(url_for("entries"))
@app.route("/entries/delete/<kind>/<int:eid>",methods=["POST"])
@shoponly
def entry_delete(kind,eid):
 if kind not in ("expense","income"):abort(404)
 db().execute("DELETE FROM "+("expenses" if kind=="expense" else "incomes")+" WHERE id=? AND shop_id=?",(eid,shopid()));db().commit();flash("Entry deleted; P&L updated.","success");return redirect(url_for("entries"))
@app.route("/customers",methods=["GET","POST"])
@shoponly
def customers():
 c=db()
 if request.method=="POST":
  cid=val("id");v=(val("name"),val("phone"),val("whatsapp"),val("address"))
  if cid:c.execute("UPDATE customers SET name=?,phone=?,whatsapp=?,address=? WHERE id=? AND shop_id=?",(*v,int(cid),shopid()))
  else:c.execute("INSERT INTO customers(shop_id,name,phone,whatsapp,address) VALUES(?,?,?,?,?)",(shopid(),*v))
  c.commit();flash("Customer saved. WhatsApp is only opened when sharing a bill.","success");return redirect(url_for("customers"))
 eid=request.args.get("edit_id",type=int);edit=c.execute("SELECT * FROM customers WHERE id=? AND shop_id=?",(eid,shopid())).fetchone() if eid else None
 cs=rows("SELECT c.*,COALESCE((SELECT SUM(due) FROM sales WHERE customer_id=c.id),0) due FROM customers c WHERE c.shop_id=? ORDER BY c.name")
 content="""<h1>Customers</h1><section class=panel><h2>{{'Edit customer' if edit else 'Add customer'}}</h2><form method=post><input type=hidden name=id value='{{edit.id if edit else \"\"}}'><div class=formgrid>{% for n,l in [('name','Name'),('phone','Phone'),('whatsapp','WhatsApp number'),('address','Address')] %}<div class=field><label>{{l}}</label><input name={{n}} value='{{edit[n] if edit else \"\"}}' {% if n=='name' %}required{% endif %}></div>{% endfor %}</div><p><button>Save customer</button></p></form></section><section class=panel><div class=tablewrap><table><thead><tr><th>Name</th><th>Phone/WhatsApp</th><th>Address</th><th>Due</th><th>Actions</th></tr></thead><tbody>{% for c in cs %}<tr><td>{{c.name}}</td><td>{{c.phone}} / {{c.whatsapp}}</td><td>{{c.address}}</td><td>{{c.due|money}}</td><td class=actions><a class=btn href='/customers?edit_id={{c.id}}'>Edit</a><form method=post action='/customers/delete/{{c.id}}' onsubmit='return confirm(\"Delete/archive customer?\")'><button class=danger>Delete</button></form></td></tr>{% endfor %}</tbody></table></div></section>"""
 return render_template_string(BASE+content+END,cs=cs,edit=edit)
@app.route("/customers/delete/<int:cid>",methods=["POST"])
@shoponly
def customer_delete(cid):
 if db().execute("SELECT 1 FROM sales WHERE customer_id=?",(cid,)).fetchone():db().execute("UPDATE customers SET name=name||' (archived)' WHERE id=? AND shop_id=?",(cid,shopid()));flash("Customer with bill history archived.","warning")
 else:db().execute("DELETE FROM customers WHERE id=? AND shop_id=?",(cid,shopid()));flash("Customer deleted.","success")
 db().commit();return redirect(url_for("customers"))
@app.route("/suppliers",methods=["GET","POST"])
@shoponly
def suppliers():
 c=db()
 if request.method=="POST":
  sid=val("id");v=(val("name"),val("phone"),val("address"))
  if sid:c.execute("UPDATE suppliers SET name=?,phone=?,address=? WHERE id=? AND shop_id=?",(*v,int(sid),shopid()))
  else:c.execute("INSERT INTO suppliers(shop_id,name,phone,address) VALUES(?,?,?,?)",(shopid(),*v))
  c.commit();flash("Supplier saved.","success");return redirect(url_for("suppliers"))
 eid=request.args.get("edit_id",type=int);edit=c.execute("SELECT * FROM suppliers WHERE id=? AND shop_id=?",(eid,shopid())).fetchone() if eid else None
 ss=rows("SELECT s.*,MAX(0,COALESCE((SELECT SUM(total) FROM purchases p WHERE p.supplier_id=s.id),0)-COALESCE((SELECT SUM(r.total) FROM purchase_returns r WHERE r.shop_id=s.shop_id AND r.supplier_name=s.name),0)) total,COALESCE((SELECT SUM(paid) FROM purchases p WHERE p.supplier_id=s.id),0) paid,MAX(0,COALESCE((SELECT SUM(due) FROM purchases p WHERE p.supplier_id=s.id),0)-COALESCE((SELECT SUM(r.total) FROM purchase_returns r WHERE r.shop_id=s.shop_id AND r.supplier_name=s.name),0)) due FROM suppliers s WHERE s.shop_id=? ORDER BY s.name")
 content="""<h1>Suppliers & Statements</h1><section class=panel><h2>{{'Edit supplier' if edit else 'Add supplier'}}</h2><form method=post><input type=hidden name=id value='{{edit.id if edit else \"\"}}'><div class=formgrid>{% for n,l in [('name','Name'),('phone','Phone'),('address','Address')] %}<div class=field><label>{{l}}</label><input name={{n}} value='{{edit[n] if edit else \"\"}}' {% if n=='name' %}required{% endif %}></div>{% endfor %}</div><p><button>Save supplier</button></p></form></section><section class=panel><div class=tablewrap><table><thead><tr><th>Supplier</th><th>Phone</th><th>Purchases</th><th>Paid</th><th>Due</th><th>Actions</th></tr></thead><tbody>{% for s in ss %}<tr><td>{{s.name}}</td><td>{{s.phone}}</td><td>{{s.total|money}}</td><td>{{s.paid|money}}</td><td>{{s.due|money}}</td><td class=actions><a class=btn href='/supplier/{{s.id}}'>View</a><a class=btn href='/suppliers?edit_id={{s.id}}'>Edit</a><form method=post action='/suppliers/delete/{{s.id}}'><button class=danger>Delete</button></form></td></tr>{% endfor %}</tbody></table></div></section>"""
 return render_template_string(BASE+content+END,ss=ss,edit=edit)
@app.route("/suppliers/delete/<int:sid>",methods=["POST"])
@shoponly
def supplier_delete(sid):
 if db().execute("SELECT 1 FROM purchases WHERE supplier_id=?",(sid,)).fetchone():db().execute("UPDATE suppliers SET name=name||' (archived)' WHERE id=? AND shop_id=?",(sid,shopid()));flash("Supplier with purchase history archived.","warning")
 else:db().execute("DELETE FROM suppliers WHERE id=? AND shop_id=?",(sid,shopid()));flash("Supplier deleted.","success")
 db().commit();return redirect(url_for("suppliers"))
@app.route("/supplier/<int:sid>")
@shoponly
def supplier_view(sid):
 s=db().execute("SELECT * FROM suppliers WHERE id=? AND shop_id=?",(sid,shopid())).fetchone()
 if not s:abort(404)
 ps=db().execute("SELECT * FROM purchases WHERE supplier_id=? AND shop_id=? ORDER BY purchase_date DESC",(sid,shopid())).fetchall()
 returns=db().execute("SELECT * FROM purchase_returns WHERE shop_id=? AND supplier_name=? ORDER BY return_date DESC",(shopid(),s["name"])).fetchall()
 return_total=sum(r["total"] or 0 for r in returns)
 content="""<section class=panel><h1>Supplier statement</h1><p><b>{{s.name}}</b> · {{s.phone}} · {{s.address}}</p><p>Purchase return / debit notes: <b>{{return_total|money}}</b></p><div class=tablewrap><table><thead><tr><th>Date</th><th>Product</th><th>Total</th><th>Paid</th><th>Due</th></tr></thead><tbody>{% for p in ps %}<tr><td>{{p.purchase_date}}</td><td>{{p.product_name}} ({{p.qty}})</td><td>{{p.total|money}}</td><td>{{p.paid|money}}</td><td>{{p.due|money}}</td></tr>{% endfor %}</tbody></table></div><button onclick=print()>Print statement</button></section>"""
 return render_template_string(BASE+content+END,s=s,ps=ps,returns=returns,return_total=return_total)
@app.route("/sales-returns",methods=["GET","POST"])
@shoponly
def sales_returns():
 c=db();sid=shopid()
 if request.method=="POST":
  sale=c.execute("SELECT * FROM sales WHERE id=? AND shop_id=?",(int(val("sale_id") or 0),sid)).fetchone()
  if not sale:flash("Select a valid sales invoice.","danger");return redirect(url_for("sales_returns"))
  try:
   pids=request.form.getlist("product_id[]");qtys=request.form.getlist("qty[]");lines=[]
   for pid,q in zip(pids,qtys):
    if not pid or float(q or 0)<=0:continue
    it=c.execute("SELECT si.*,p.name current_name FROM sale_items si LEFT JOIN products p ON p.id=si.product_id WHERE si.sale_id=? AND si.product_id=?",(sale["id"],int(pid))).fetchone()
    already=c.execute("SELECT COALESCE(SUM(ri.qty),0) n FROM sales_return_items ri JOIN sales_returns r ON r.id=ri.return_id WHERE r.sale_id=? AND ri.product_id=?",(sale["id"],int(pid))).fetchone()["n"]
    if not it or float(q)>it["qty"]-already+0.00001:raise ValueError("Return quantity exceeds the original sold quantity less previous returns.")
    lines.append((it,float(q)))
   if not lines:raise ValueError("Choose at least one item to return.")
   total=sum(q*it["price"] for it,q in lines);no="SR-"+datetime.now().strftime("%y%m%d-%H%M%S")
   cur=c.execute("INSERT INTO sales_returns(shop_id,return_no,return_date,sale_id,customer_name,reason,total,notes) VALUES(?,?,?,?,?,?,?,?)",(sid,no,val("return_date",today()),sale["id"],sale["customer_name"],val("reason","Customer return"),total,val("notes")));rid=cur.lastrowid
   for it,q in lines:
    c.execute("INSERT INTO sales_return_items(return_id,product_id,product_name,qty,unit_price,line_total,cost) VALUES(?,?,?,?,?,?,?)",(rid,it["product_id"],it["product_name"],q,it["price"],q*it["price"],it["cost"] or 0))
    c.execute("UPDATE products SET stock=stock+? WHERE id=? AND shop_id=?",(q,it["product_id"],sid))
   c.commit();flash(f"Sales return {no} posted. Stock increased and return is deducted from net sales.","success");return redirect(url_for("sales_returns"))
  except Exception as e:c.rollback();flash(str(e),"danger");return redirect(url_for("sales_returns"))
 sales=c.execute("SELECT * FROM sales WHERE shop_id=? ORDER BY id DESC LIMIT 200",(sid,)).fetchall();returns=c.execute("SELECT * FROM sales_returns WHERE shop_id=? ORDER BY id DESC",(sid,)).fetchall()
 content="""<h1>Sales Returns / Credit Notes</h1><section class=panel><p>Post only items actually returned. Return quantity is checked against the original invoice and earlier returns; returned goods are added back to stock.</p><form method=post><div class=formgrid><div class=field><label>Original invoice</label><select name=sale_id id=sale required onchange=loadItems()><option value=''>Select invoice</option>{% for s in sales %}<option value={{s.id}}>{{s.invoice}} · {{s.customer_name}} · {{s.sale_date}} · {{s.total|money}}</option>{% endfor %}</select></div><div class=field><label>Return date</label><input type=date name=return_date value='{{today()}}' required></div><div class=field><label>Reason</label><select name=reason><option>Damaged</option><option>Expired</option><option>Wrong item</option><option>Customer return</option><option>Other</option></select></div><div class=field><label>Notes</label><input name=notes></div></div><div class=tablewrap><table><thead><tr><th>Item</th><th>Sold qty</th><th>Return qty</th></tr></thead><tbody id=return-lines></tbody></table></div><p><button>Post sales return</button></p></form></section><section class=panel><h2>Return history</h2><table><thead><tr><th>Date</th><th>Credit note</th><th>Original invoice</th><th>Customer</th><th>Reason</th><th>Total</th></tr></thead><tbody>{% for r in returns %}<tr><td>{{r.return_date}}</td><td>{{r.return_no}}</td><td>{{r.sale_id}}</td><td>{{r.customer_name}}</td><td>{{r.reason}}</td><td>{{r.total|money}}</td></tr>{% else %}<tr><td colspan=6>No returns posted.</td></tr>{% endfor %}</tbody></table></section><script>const saleData={{sale_data|tojson}};function loadItems(){let id=document.getElementById('sale').value,body=document.getElementById('return-lines');body.innerHTML='';(saleData[id]||[]).forEach(i=>{let r=document.createElement('tr');r.innerHTML='<td>'+i.name+'<input type=hidden name="product_id[]" value="'+i.product_id+'"></td><td>'+i.remaining+'</td><td><input name="qty[]" type=number min=0 step=.001 max="'+i.remaining+'" value=0></td>';body.appendChild(r)})}</script>"""
 data={}
 for sale in sales:
  its=c.execute("SELECT si.product_id,si.product_name,si.qty,COALESCE((SELECT SUM(ri.qty) FROM sales_return_items ri JOIN sales_returns sr ON sr.id=ri.return_id WHERE sr.sale_id=si.sale_id AND ri.product_id=si.product_id),0) returned FROM sale_items si WHERE si.sale_id=?",(sale["id"],)).fetchall()
  data[str(sale["id"])]=[{"product_id":i["product_id"],"name":i["product_name"],"remaining":max(0,(i["qty"] or 0)-(i["returned"] or 0))} for i in its if i["product_id"]]
 return render_template_string(BASE+content+END,sales=sales,returns=returns,sale_data=data)

@app.route("/purchase-returns",methods=["GET","POST"])
@shoponly
def purchase_returns():
 c=db();sid=shopid()
 if request.method=="POST":
  try:
   pid=int(val("purchase_id") or 0);p=c.execute("SELECT * FROM purchases WHERE id=? AND shop_id=?",(pid,sid)).fetchone();q=num("qty")
   if not p or q<=0:raise ValueError("Select a purchase line and enter a positive return quantity.")
   prev=c.execute("SELECT COALESCE(SUM(i.qty),0) n FROM purchase_return_items i JOIN purchase_returns r ON r.id=i.return_id WHERE r.purchase_id=?",(pid,)).fetchone()["n"]
   if q>p["qty"]-prev+0.00001:raise ValueError("Return quantity exceeds purchased quantity less earlier returns.")
   if q>(getp(p["product_id"])["stock"] if getp(p["product_id"]) else 0):raise ValueError("Cannot return more than current stock.")
   total=q*p["unit_cost"];no="PR-"+datetime.now().strftime("%y%m%d-%H%M%S")
   cur=c.execute("INSERT INTO purchase_returns(shop_id,return_no,return_date,purchase_id,supplier_name,reason,total,notes) VALUES(?,?,?,?,?,?,?,?)",(sid,no,val("return_date",today()),pid,p["supplier_name"],val("reason","Supplier return"),total,val("notes")));rid=cur.lastrowid
   c.execute("INSERT INTO purchase_return_items(return_id,product_id,product_name,qty,unit_cost,line_total) VALUES(?,?,?,?,?,?)",(rid,p["product_id"],p["product_name"],q,p["unit_cost"],total))
   c.execute("UPDATE products SET stock=stock-? WHERE id=? AND shop_id=?",(q,p["product_id"],sid));c.commit();flash(f"Purchase return {no} posted. Stock reduced; supplier balance should be reconciled against credit note.","success");return redirect(url_for("purchase_returns"))
  except Exception as e:c.rollback();flash(str(e),"danger");return redirect(url_for("purchase_returns"))
 purchases=c.execute("SELECT * FROM purchases WHERE shop_id=? ORDER BY id DESC LIMIT 300",(sid,)).fetchall();returns=c.execute("SELECT * FROM purchase_returns WHERE shop_id=? ORDER BY id DESC",(sid,)).fetchall()
 content="""<h1>Purchase Returns / Supplier Debit Notes</h1><section class=panel><p>Record goods returned to suppliers. The system validates against original purchase quantity and available stock. Reconcile the credit with supplier balances/payment records.</p><form method=post><div class=formgrid><div class=field><label>Original purchase line</label><select name=purchase_id required>{% for p in purchases %}<option value={{p.id}}>{{p.invoice_no}} · {{p.product_name}} · Batch {{p.batch_no or "—"}} · {{p.supplier_name}} · Qty {{p.qty}} · {{p.purchase_date}}</option>{% endfor %}</select></div><div class=field><label>Return date</label><input type=date name=return_date value='{{today()}}' required></div><div class=field><label>Return quantity</label><input name=qty type=number min=.001 step=.001 required></div><div class=field><label>Reason</label><select name=reason><option>Damaged</option><option>Expired</option><option>Wrong item</option><option>Supplier return</option><option>Other</option></select></div><div class=field><label>Notes</label><input name=notes></div></div><button>Post purchase return</button></form></section><section class=panel><h2>Return history</h2><table><thead><tr><th>Date</th><th>Debit note</th><th>Purchase line</th><th>Supplier</th><th>Reason</th><th>Total</th></tr></thead><tbody>{% for r in returns %}<tr><td>{{r.return_date}}</td><td>{{r.return_no}}</td><td>{{r.purchase_id}}</td><td>{{r.supplier_name}}</td><td>{{r.reason}}</td><td>{{r.total|money}}</td></tr>{% else %}<tr><td colspan=6>No returns posted.</td></tr>{% endfor %}</tbody></table></section>"""
 return render_template_string(BASE+content+END,purchases=purchases,returns=returns)

@app.route("/payment-methods",methods=["GET","POST"])
@shoponly
def payment_methods():
 c=db();sid=shopid()
 if request.method=="POST":
  name=val("name")
  if not name:flash("Enter a payment method name.","danger")
  else:
   try:c.execute("INSERT INTO payment_methods(shop_id,name) VALUES(?,?)",(sid,name));c.commit();flash("Payment method added.","success")
   except sqlite3.IntegrityError:flash("That payment method already exists.","warning")
  return redirect(url_for("payment_methods"))
 methods=c.execute("SELECT * FROM payment_methods WHERE shop_id=? ORDER BY active DESC,name",(sid,)).fetchall()
 content="""<h1>Payment Methods</h1><section class=panel><h2>Add payment method</h2><form method=post class=formgrid><div class=field><label>Method name</label><input name=name placeholder='e.g. PhonePe, Google Pay, Bank Transfer' required></div><div><button>Add method</button></div></form></section><section class=panel><h2>Available methods</h2><table><thead><tr><th>Name</th><th>Status</th><th>Action</th></tr></thead><tbody>{% for m in methods %}<tr><td>{{m.name}}</td><td>{{'Active' if m.active else 'Inactive'}}</td><td><form method=post action=/payment-methods/{{m.id}}/toggle><button class=btn2>{{'Disable' if m.active else 'Enable'}}</button></form></td></tr>{% endfor %}</tbody></table></section>"""
 return render_template_string(BASE+content+END,methods=methods)

@app.route("/payment-methods/<int:mid>/toggle",methods=["POST"])
@shoponly
def payment_method_toggle(mid):
 c=db();m=c.execute("SELECT * FROM payment_methods WHERE id=? AND shop_id=?",(mid,shopid())).fetchone()
 if m:c.execute("UPDATE payment_methods SET active=? WHERE id=? AND shop_id=?",(0 if m["active"] else 1,mid,shopid()));c.commit();flash("Payment method updated.","success")
 return redirect(url_for("payment_methods"))

@app.route("/reports")
@shoponly
def reports():
 frm=request.args.get("from",date.today().replace(day=1).isoformat());to=request.args.get("to",today());sid=shopid();c=db()
 sales=c.execute("SELECT * FROM sales WHERE shop_id=? AND sale_date BETWEEN ? AND ?",(sid,frm,to)).fetchall()
 exp=c.execute("SELECT * FROM expenses WHERE shop_id=? AND entry_date BETWEEN ? AND ?",(sid,frm,to)).fetchall()
 inc=c.execute("SELECT * FROM incomes WHERE shop_id=? AND entry_date BETWEEN ? AND ?",(sid,frm,to)).fetchall()
 rev=sum(x["total"] or 0 for x in sales); sales_returns=c.execute("SELECT COALESCE(SUM(total),0) n FROM sales_returns WHERE shop_id=? AND return_date BETWEEN ? AND ?",(sid,frm,to)).fetchone()["n"]; cogs=sum(x["cost_total"] or 0 for x in sales)-c.execute("SELECT COALESCE(SUM(i.cost*i.qty),0) n FROM sales_return_items i JOIN sales_returns r ON r.id=i.return_id WHERE r.shop_id=? AND r.return_date BETWEEN ? AND ?",(sid,frm,to)).fetchone()["n"];other=sum(x["amount"] or 0 for x in inc);opex=sum(x["amount"] or 0 for x in exp);net_sales=rev-sales_returns;gross=net_sales-cogs;net=gross+other-opex
 methods=c.execute("SELECT payment_method,SUM(total) total FROM sales WHERE shop_id=? AND sale_date BETWEEN ? AND ? GROUP BY payment_method",(sid,frm,to)).fetchall()
 content="""<h1>Accounts & Reports — Profit & Loss</h1><section class=panel><form class=formgrid><div class=field><label>From</label><input type=date name=from value={{frm}}></div><div class=field><label>To</label><input type=date name=to value={{to}}></div><div><button>Run report</button></div></form></section><div class=grid>{% for n,v in [('Gross sales',rev),('Less: Sales returns',sales_returns),('Net sales',net_sales),('Cost of goods sold',cogs),('Gross profit',gross),('Other income',other),('Operating expenses',opex),('Net profit / loss',net)] %}<div class=metric><span class=muted>{{n}}</span><b>{{v|money}}</b></div>{% endfor %}</div><section class=panel><h2>Profit & Loss</h2><table><tr><th>Gross sales</th><td>{{rev|money}}</td></tr><tr><th>Less: Sales returns</th><td>({{sales_returns|money}})</td></tr><tr><th>Net sales</th><td>{{net_sales|money}}</td></tr><tr><th>Less: Cost of goods sold</th><td>({{cogs|money}})</td></tr><tr><th>Gross profit</th><td>{{gross|money}}</td></tr><tr><th>Add: Other income</th><td>{{other|money}}</td></tr><tr><th>Less: Expenses</th><td>({{opex|money}})</td></tr><tr><th>Estimated net profit / loss</th><th>{{net|money}}</th></tr></table><p class=muted>Management report only. COGS uses saved product costs on sales. This is not a complete double-entry ledger or GST filing tool.</p></section><section class=panel><h2>Sales by payment method</h2>{% for m in methods %}<p>{{m.payment_method}} <b style='float:right'>{{m.total|money}}</b></p>{% else %}<p>No sales.</p>{% endfor %}</section>"""
 return render_template_string(BASE+content+END,frm=frm,to=to,rev=rev,sales_returns=sales_returns,net_sales=net_sales,cogs=cogs,gross=gross,other=other,opex=opex,net=net,methods=methods)
@app.route("/backup")
@shoponly
def backup():
 data=shopbackup(shopid())
 return send_file(io.BytesIO(json.dumps(data,indent=2).encode()),mimetype="application/json",as_attachment=True,download_name="easykada-shop-backup.json")
@app.errorhandler(500)
def internal_error(e):
    """Return a simple error page without invoking the normal template renderer.

    Keeping this handler independent of page() prevents a secondary template
    exception from masking the original request failure.
    """
    original = getattr(e, "original_exception", None)
    if original is not None:
        logging.error(
            "Unhandled EasyKada exception",
            exc_info=(type(original), original, original.__traceback__),
        )
    else:
        logging.exception("EasyKada HTTP 500 handler invoked: %s", e)
    try:
        db().rollback()
    except Exception:
        pass
    from flask import Response
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EasyKada error</title><style>body{font:16px system-ui;background:#f4f7fb;color:#172b24;margin:0;padding:24px}.card{max-width:620px;margin:8vh auto;background:white;padding:24px;border-radius:14px;border:1px solid #ddd}a{display:inline-block;background:#126b4f;color:white;padding:10px 14px;border-radius:8px;text-decoration:none}</style></head><body><main class="card"><h1>500 — Internal Server Error</h1><p>EasyKada could not complete this request. The detailed exception has been written to the hosting runtime logs.</p><p><a href="/">Return to EasyKada</a></p></main></body></html>"""
    return Response(html, status=500, content_type="text/html; charset=utf-8")
@app.errorhandler(403)
def forbidden(e):return page("<section class=panel><h1>403</h1><p>Access denied.</p></section>"),403
@app.errorhandler(404)
def notfound(e):return page("<section class=panel><h1>404</h1><p>Record or page not found.</p></section>"),404
with app.app_context():init()
if __name__=="__main__":app.run(host=os.environ.get("HOST","127.0.0.1"),port=int(os.environ.get("PORT","5000")),debug=False)
