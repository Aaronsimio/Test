"""Catálogo minimalista con control de stock.
Uso:  pip install flask  ->  python app.py  ->  http://localhost:5000
Admin: http://localhost:5000/admin   (usuario: admin / clave: variable ADMIN_PASSWORD, por defecto "1z2x3c")
"""
import csv, io, os, sqlite3, uuid
from flask import Flask, g, request, redirect, url_for, render_template, abort, Response, flash
from jinja2 import DictLoader
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "catalogo.db")
UPLOADS = os.path.join(BASE, "static", "uploads")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "1z2x3c")
# Número de WhatsApp que recibe los pedidos: código de país + área + número, sin "+" ni espacios.
# Ejemplo Argentina (móvil): 5491123456789
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "541133688787")
EXT = {"jpg", "jpeg", "png", "webp"}
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambiar-esta-clave")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


with sqlite3.connect(DB) as c:
    c.execute("""CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT NOT NULL,
        description TEXT DEFAULT '', price REAL DEFAULT 0, stock INTEGER DEFAULT 0,
        images TEXT DEFAULT '')""")


@app.template_filter("imgs")
def imgs(p):
    return [i for i in (p["images"] or "").split(",") if i]


@app.template_filter("money")
def money(v):
    return "$ {:,.2f}".format(v).replace(",", "X").replace(".", ",").replace("X", ".")


@app.after_request
def no_cache(r):
    if r.mimetype == "text/html":
        r.headers["Cache-Control"] = "no-store"
    return r


@app.before_request
def auth():
    if request.path.startswith("/admin"):
        a = request.authorization
        if not a or a.password != ADMIN_PASSWORD:
            return Response("Acceso restringido", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})


# ---------- Público ----------
@app.route("/")
def home():
    cats = db().execute("""SELECT category, COUNT(*) n,
        (SELECT images FROM products p2 WHERE p2.category=p.category AND images!='' LIMIT 1) cover
        FROM products p GROUP BY category ORDER BY category""").fetchall()
    return render_template("home", cats=cats)


@app.route("/c/<cat>")
def category(cat):
    items = db().execute("SELECT * FROM products WHERE category=? ORDER BY name", (cat,)).fetchall()
    if not items:
        abort(404)
    return render_template("category", cat=cat, items=items)


@app.route("/p/<int:pid>")
def product(pid):
    p = db().execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    return render_template("product", p=p)


@app.errorhandler(404)
def nf(_):
    return render_template("notfound"), 404


@app.route("/carrito")
def cart():
    return render_template("cart", wa=WHATSAPP_NUMBER)


# ---------- Administración ----------
@app.route("/admin")
def admin():
    items = db().execute("SELECT * FROM products ORDER BY category, name").fetchall()
    return render_template("admin", items=items)


def save_images(old=""):
    keep = [i for i in old.split(",") if i]
    for f in request.files.getlist("images"):
        if f and f.filename and f.filename.rsplit(".", 1)[-1].lower() in EXT and len(keep) < 3:
            name = uuid.uuid4().hex[:12] + "_" + secure_filename(f.filename)
            f.save(os.path.join(UPLOADS, name))
            keep.append(name)
    return keep


@app.route("/admin/new", methods=["GET", "POST"])
@app.route("/admin/edit/<int:pid>", methods=["GET", "POST"])
def edit(pid=None):
    p = db().execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone() if pid else None
    if pid and not p:
        abort(404)
    if request.method == "POST":
        f = request.form
        images = save_images(p["images"] if p else "")
        for r in request.form.getlist("remove"):
            if r in images:
                images.remove(r)
                try:
                    os.remove(os.path.join(UPLOADS, r))
                except OSError:
                    pass
        vals = (f["name"].strip(), f["category"].strip(), f.get("description", "").strip(),
                float(f.get("price") or 0), int(f.get("stock") or 0), ",".join(images))
        if p:
            db().execute("UPDATE products SET name=?,category=?,description=?,price=?,stock=?,images=? WHERE id=?", vals + (pid,))
        else:
            db().execute("INSERT INTO products(name,category,description,price,stock,images) VALUES(?,?,?,?,?,?)", vals)
        db().commit()
        flash("Guardado")
        return redirect(url_for("admin"))
    cats = [r[0] for r in db().execute("SELECT DISTINCT category FROM products")]
    return render_template("form", p=p, cats=cats)


@app.post("/admin/stock/<int:pid>")
def stock(pid):
    db().execute("UPDATE products SET stock=? WHERE id=?", (max(0, int(request.form["stock"])), pid))
    db().commit()
    flash("Stock actualizado")
    return redirect(url_for("admin"))


@app.post("/admin/delete/<int:pid>")
def delete(pid):
    db().execute("DELETE FROM products WHERE id=?", (pid,))
    db().commit()
    flash("Artículo eliminado")
    return redirect(url_for("admin"))


@app.post("/admin/import")
def import_csv():
    """Columnas: nombre;categoria;descripcion;precio;stock  (coma o punto y coma).
    Si el nombre ya existe en esa categoría, actualiza precio, descripción y stock."""
    raw = request.files["file"].read().decode("utf-8-sig")
    dialect = csv.Sniffer().sniff(raw[:2000], delimiters=",;")
    n = 0
    for r in csv.DictReader(io.StringIO(raw), dialect=dialect):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        if not r.get("nombre") or not r.get("categoria"):
            continue
        price = float((r.get("precio") or "0").replace(",", "."))
        stk = int(float(r.get("stock") or 0))
        ex = db().execute("SELECT id FROM products WHERE name=? AND category=?", (r["nombre"], r["categoria"])).fetchone()
        if ex:
            db().execute("UPDATE products SET description=?,price=?,stock=? WHERE id=?",
                         (r.get("descripcion", ""), price, stk, ex["id"]))
        else:
            db().execute("INSERT INTO products(name,category,description,price,stock) VALUES(?,?,?,?,?)",
                         (r["nombre"], r["categoria"], r.get("descripcion", ""), price, stk))
        n += 1
    db().commit()
    flash(f"{n} filas importadas")
    return redirect(url_for("admin"))


@app.route("/admin/export.csv")
def export_csv():
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["nombre", "categoria", "descripcion", "precio", "stock"])
    for p in db().execute("SELECT * FROM products ORDER BY category, name"):
        w.writerow([p["name"], p["category"], p["description"], p["price"], p["stock"]])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=stock.csv"})


# ---------- Plantillas ----------
CSS = """
:root{--bg:#f7f7f6;--ink:#1d1d1f;--muted:#6e6e73;--line:#e4e4e2;--card:#fff;--ok:#3f6b4f;--warn:#9a6a1f;--no:#8a3b3b}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--ink:#ededeb;--muted:#9b9b98;--line:#2c2c2b;--card:#1e1e1e;--ok:#8fc2a0;--warn:#d9b26a;--no:#d58a8a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 'Manrope',system-ui,-apple-system,'Segoe UI',sans-serif}
a{color:inherit;text-decoration:none}
header,main,footer{max-width:1100px;margin:0 auto;padding:0 24px}
header{display:flex;justify-content:space-between;align-items:baseline;padding-top:28px;padding-bottom:28px}
.brand{font-weight:600;letter-spacing:.01em;font-size:1.15rem}
nav a{color:var(--muted);margin-left:20px;font-size:.92rem}nav a:hover{color:var(--ink)}
h1{font-weight:500;font-size:clamp(1.8rem,4vw,2.6rem);line-height:1.15;margin:.4em 0 .2em;letter-spacing:-.02em}
h2{font-weight:500;font-size:1.1rem;margin:0}
.sub{color:var(--muted);margin:0 0 36px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:28px 22px;padding-bottom:64px}
.tile .ph{aspect-ratio:4/5;background:var(--line);overflow:hidden;border-radius:4px}
.tile img{width:100%;height:100%;object-fit:cover;display:block;transition:opacity .2s}
.tile:hover img{opacity:.88}
.tile h2{margin-top:12px}.tile small{color:var(--muted)}
.empty{color:var(--muted);padding:40px 0}
.detail{display:grid;grid-template-columns:1.1fr 1fr;gap:48px;padding-bottom:72px}
.main-img{aspect-ratio:1/1;background:var(--line);border-radius:4px;overflow:hidden}
.main-img img{width:100%;height:100%;object-fit:cover;display:block}
.thumbs{display:flex;gap:10px;margin-top:10px}
.thumbs img{width:72px;height:72px;object-fit:cover;border-radius:3px;cursor:pointer;opacity:.6;border:1px solid transparent}
.thumbs img.on,.thumbs img:hover{opacity:1;border-color:var(--ink)}
.price{font-size:1.5rem;margin:.3em 0 .6em}
.badge{display:inline-block;font-size:.88rem;padding:3px 12px;border:1px solid currentColor;border-radius:99px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.no{color:var(--no)}
.desc{color:var(--muted);max-width:60ch;white-space:pre-line;margin-top:24px}
.crumb{color:var(--muted);font-size:.9rem;margin-bottom:12px;display:block}
/* admin */
.flash{background:var(--card);border:1px solid var(--line);padding:10px 16px;border-radius:4px;margin-bottom:20px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);font-size:.93rem}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-weight:500;color:var(--muted)}
.wrap{overflow-x:auto;margin-bottom:32px}
input,select,textarea{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:4px;padding:8px 10px;width:100%}
td input[type=number]{width:80px}
button,.btn{font:inherit;cursor:pointer;background:var(--ink);color:var(--bg);border:0;border-radius:4px;padding:8px 16px;display:inline-block}
button.ghost,.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:24px}
form.f{display:grid;gap:16px;max-width:560px;padding-bottom:64px}
label{display:grid;gap:6px;color:var(--muted);font-size:.9rem}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.cur{display:flex;gap:10px;flex-wrap:wrap}.cur label{font-size:.8rem;justify-items:center}.cur img{width:72px;height:72px;object-fit:cover;border-radius:3px}
footer{padding-top:24px;padding-bottom:40px;color:var(--muted);font-size:.85rem;border-top:1px solid var(--line)}
@media(max-width:760px){.detail{grid-template-columns:1fr;gap:24px}.grid{grid-template-columns:repeat(2,1fr);gap:20px 14px}}
"""

LAYOUT = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{% block title %}Catálogo{% endblock %}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600&display=swap" rel="stylesheet">
<style>""" + CSS + """</style></head><body>
<header><a class="brand" href="/">Catálogo</a><nav><a href="/">Categorías</a><a href="/carrito">Carrito <span id="cn"></span></a><a href="/admin">Administrar</a></nav></header>
<script>
const C={get(){try{return JSON.parse(localStorage.getItem('cart')||'{}')}catch(e){return {}}},
set(c){try{localStorage.setItem('cart',JSON.stringify(c))}catch(e){}C.badge()},
add(p,q){const c=C.get(),o=c[p.id]||{qty:0};o.id=p.id;o.name=p.name;o.price=p.price;o.max=p.max;o.qty=Math.min(p.max,o.qty+q);c[p.id]=o;C.set(c)},
badge(){const n=Object.values(C.get()).reduce((a,b)=>a+b.qty,0);document.getElementById('cn').textContent=n?'('+n+')':''}};
C.badge();
</script>
<main>{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}{% block body %}{% endblock %}</main>
<footer>© Catálogo</footer></body></html>"""

STOCK = """{% macro stock(p) %}{% if p.stock<=0 %}<span class="badge no">Agotado</span>
{% elif p.stock<=3 %}<span class="badge warn">Últimas {{ p.stock }} unidades</span>
{% else %}<span class="badge ok">Disponible</span>{% endif %}{% endmacro %}"""

T = {
"layout": LAYOUT,
"home": """{% extends 'layout' %}{% block body %}<h1>Categorías</h1><p class="sub">Elegí una categoría para ver los artículos.</p>
{% if not cats %}<p class="empty">Todavía no hay artículos. <a href="/admin/new"><u>Cargá el primero</u></a>.</p>{% endif %}
<div class="grid">{% for c in cats %}<a class="tile" href="{{ url_for('category',cat=c.category) }}">
<div class="ph">{% if c.cover %}<img loading="lazy" src="/static/uploads/{{ c.cover.split(',')[0] }}" alt="">{% endif %}</div>
<h2>{{ c.category }}</h2><small>{{ c.n }} artículo{{ '' if c.n==1 else 's' }}</small></a>{% endfor %}</div>{% endblock %}""",
"category": """{% extends 'layout' %}{% import 'stock' as s %}{% block title %}{{ cat }}{% endblock %}{% block body %}
<a class="crumb" href="/">Categorías</a><h1>{{ cat }}</h1><p class="sub">{{ items|length }} artículos</p>
<div class="grid">{% for p in items %}<a class="tile" href="{{ url_for('product',pid=p.id) }}">
<div class="ph">{% if p|imgs %}<img loading="lazy" src="/static/uploads/{{ (p|imgs)[0] }}" alt="{{ p.name }}">{% endif %}</div>
<h2>{{ p.name }}</h2><small>{{ p.price|money }}</small><br>{{ s.stock(p) }}</a>{% endfor %}</div>{% endblock %}""",
"product": """{% extends 'layout' %}{% import 'stock' as s %}{% block title %}{{ p.name }}{% endblock %}{% block body %}
<a class="crumb" href="{{ url_for('category',cat=p.category) }}">← {{ p.category }}</a>
<div class="detail"><div>{% set im = p|imgs %}<div class="main-img">{% if im %}<img id="big" src="/static/uploads/{{ im[0] }}" alt="{{ p.name }}">{% endif %}</div>
{% if im|length>1 %}<div class="thumbs">{% for i in im %}<img class="{{ 'on' if loop.first }}" src="/static/uploads/{{ i }}" alt="" onclick="big.src=this.src;document.querySelectorAll('.thumbs img').forEach(x=>x.classList.remove('on'));this.classList.add('on')">{% endfor %}</div>{% endif %}</div>
<div><h1>{{ p.name }}</h1><div class="price">{{ p.price|money }}</div>{{ s.stock(p) }}
{% if p.stock>0 %}<div class="row" style="margin:24px 0 0"><input type="number" id="q" min="1" max="{{ p.stock }}" value="1" style="width:80px" aria-label="Cantidad">
<button onclick="addCart()">Agregar al carrito</button></div><p id="added" class="sub" style="margin:10px 0 0"></p>
<script>function addCart(){var m={{ p.stock }};var q=Math.max(1,Math.min(m,parseInt(document.getElementById('q').value)||1));
C.add({id:{{ p.id }},name:{{ p.name|tojson }},price:{{ p.price }},max:m},q);
document.getElementById('added').innerHTML='Agregado al carrito. <a href="/carrito"><u>Ver carrito</u></a>'}</script>{% endif %}
<p class="desc">{{ p.description }}</p></div></div>{% endblock %}""",
"notfound": "{% extends 'layout' %}{% block body %}<h1>No encontrado</h1><p class=\"sub\"><a href=\"/\"><u>Volver al inicio</u></a></p>{% endblock %}",
"admin": """{% extends 'layout' %}{% block title %}Administrar{% endblock %}{% block body %}<h1>Administrar</h1><p class="sub">Altas, stock e importación.</p>
<div class="row"><a class="btn" href="/admin/new">Nuevo artículo</a><a class="btn ghost" href="/admin/export.csv">Exportar CSV</a>
<form method="post" action="/admin/import" enctype="multipart/form-data" class="row" style="margin:0">
<input type="file" name="file" accept=".csv" required style="width:auto"><button class="ghost">Importar CSV</button></form></div>
<p class="sub" style="margin-bottom:16px">Columnas del CSV: nombre; categoria; descripcion; precio; stock. Si el artículo ya existe, se actualiza.</p>
<div class="wrap"><table><tr><th>Artículo</th><th>Categoría</th><th>Precio</th><th>Stock</th><th></th></tr>
{% for p in items %}<tr><td>{{ p.name }}</td><td>{{ p.category }}</td><td>{{ p.price|money }}</td>
<td><form method="post" action="/admin/stock/{{ p.id }}" class="row" style="margin:0"><input type="number" min="0" name="stock" value="{{ p.stock }}"><button class="ghost">Guardar</button></form></td>
<td style="white-space:nowrap"><a href="/admin/edit/{{ p.id }}"><u>Editar</u></a>
<form method="post" action="/admin/delete/{{ p.id }}" style="display:inline" onsubmit="return confirm('¿Eliminar este artículo?')"><button class="ghost" style="margin-left:8px">Eliminar</button></form></td></tr>
{% else %}<tr><td colspan="5" class="empty">Sin artículos.</td></tr>{% endfor %}</table></div>{% endblock %}""",
"form": """{% extends 'layout' %}{% block title %}Artículo{% endblock %}{% block body %}<h1>{{ 'Editar' if p else 'Nuevo' }} artículo</h1><p class="sub">Hasta 3 imágenes por artículo.</p>
<form class="f" method="post" enctype="multipart/form-data">
<label>Nombre<input name="name" required value="{{ p.name if p }}"></label>
<label>Categoría<input name="category" required list="cats" value="{{ p.category if p }}"></label>
<datalist id="cats">{% for c in cats %}<option value="{{ c }}">{% endfor %}</datalist>
<div class="cols"><label>Precio<input type="number" step="0.01" min="0" name="price" value="{{ p.price if p else 0 }}"></label>
<label>Stock<input type="number" min="0" name="stock" value="{{ p.stock if p else 0 }}"></label></div>
<label>Descripción<textarea name="description" rows="5">{{ p.description if p }}</textarea></label>
{% if p and p|imgs %}<div class="cur">{% for i in p|imgs %}<label><img src="/static/uploads/{{ i }}" alt=""><span><input type="checkbox" name="remove" value="{{ i }}" style="width:auto"> quitar</span></label>{% endfor %}</div>{% endif %}
<label>Agregar imágenes (JPG, PNG o WebP)<input type="file" name="images" accept="image/*" multiple></label>
<div class="row"><button>Guardar cambios</button><a class="btn ghost" href="/admin">Cancelar</a></div></form>{% endblock %}""",
"cart": """{% extends 'layout' %}{% block title %}Carrito{% endblock %}{% block body %}
<h1>Carrito</h1><p class="sub">Revisá tu selección y enviá la consulta por WhatsApp.</p>
<div id="empty" class="empty" hidden>Tu carrito está vacío. <a href="/"><u>Ver categorías</u></a></div>
<div id="cart" hidden><div class="wrap"><table id="rows"></table></div>
<div style="display:grid;gap:16px;max-width:560px;padding-bottom:64px"><p class="price" id="total" style="margin:0"></p>
<label>Tu nombre<input id="nm" placeholder="Nombre y apellido" autocomplete="name"></label>
<button onclick="send()">Enviar consulta por WhatsApp</button></div></div>
<script>
var WA="{{ wa }}";
var money=function(v){return '$ '+v.toLocaleString('es-AR',{minimumFractionDigits:2,maximumFractionDigits:2})};
var esc=function(s){return String(s).replace(/[&<>"]/g,function(m){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]})};
function draw(){var c=C.get(),ids=Object.keys(c),t=0;
document.getElementById('empty').hidden=ids.length>0;document.getElementById('cart').hidden=!ids.length;
document.getElementById('rows').innerHTML='<tr><th>Artículo</th><th>Cant.</th><th>Subtotal</th><th></th></tr>'+ids.map(function(i){var o=c[i];t+=o.price*o.qty;
return '<tr><td><a href="/p/'+i+'">'+esc(o.name)+'</a><br><small>'+money(o.price)+'</small></td><td><input type="number" min="1" max="'+o.max+'" value="'+o.qty+'" onchange="setQ('+i+',this.value)"></td><td>'+money(o.price*o.qty)+'</td><td><button class="ghost" onclick="rm('+i+')">Quitar</button></td></tr>'}).join('');
document.getElementById('total').textContent='Total estimado: '+money(t)}
function setQ(i,v){var c=C.get();c[i].qty=Math.max(1,Math.min(c[i].max,parseInt(v)||1));C.set(c);draw()}
function rm(i){var c=C.get();delete c[i];C.set(c);draw()}
function send(){var n=document.getElementById('nm').value.trim();
if(!n){document.getElementById('nm').focus();return}
var t=0,l=Object.values(C.get()).map(function(o){t+=o.price*o.qty;return '• '+o.qty+' x '+o.name+' ('+money(o.price)+' c/u)'}).join('\\n');
var m='Hola, soy '+n+'. Quisiera consultar por los siguientes artículos:\\n\\n'+l+'\\n\\nTotal estimado: '+money(t);
window.open('https://wa.me/'+WA+'?text='+encodeURIComponent(m),'_blank')}
document.addEventListener('DOMContentLoaded',draw);
</script>{% endblock %}""",
"stock": STOCK,
}
app.jinja_loader = DictLoader(T)

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
