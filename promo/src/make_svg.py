# -*- coding: utf-8 -*-
"""Генерирует слоёные SVG-версии баннеров для импорта в Figma.
Каждый блок — именованная <g> (в Figma станет группой/слоем), текст остаётся
редактируемым текстом, лого вшито как data-URI PNG, орбы/карточки — вектор."""
import base64, os, html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # D:/MM/promo
MM = os.path.dirname(ROOT)  # D:/MM
OUT = os.path.join(ROOT, "figma")
os.makedirs(OUT, exist_ok=True)

with open(os.path.join(MM, "logo.png"), "rb") as f:
    LOGO = "data:image/png;base64," + base64.b64encode(f.read()).decode()

def esc(s): return html.escape(s, quote=True)

# общие defs: орбы + золотые градиенты + шрифты
FONTS = ("@import url('https://fonts.googleapis.com/css2?"
         "family=Cinzel:wght@700;800&family=Manrope:wght@500;600;700;800&display=swap');")

DEFS = f"""<defs>
  <style><![CDATA[{FONTS}]]></style>
  <radialGradient id="orbGold" cx="50%" cy="50%" r="50%">
    <stop offset="0%" stop-color="#d4af37" stop-opacity="0.16"/>
    <stop offset="62%" stop-color="#d4af37" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="orbAmber" cx="50%" cy="50%" r="50%">
    <stop offset="0%" stop-color="#8c3c14" stop-opacity="0.14"/>
    <stop offset="64%" stop-color="#8c3c14" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="orbRose" cx="50%" cy="50%" r="50%">
    <stop offset="0%" stop-color="#78142a" stop-opacity="0.16"/>
    <stop offset="64%" stop-color="#78142a" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="gold" x1="0%" y1="0%" x2="100%" y2="100%">
    <stop offset="0%" stop-color="#f6dd8b"/>
    <stop offset="50%" stop-color="#d4af37"/>
    <stop offset="100%" stop-color="#c9971d"/>
  </linearGradient>
  <linearGradient id="goldBig" x1="0%" y1="0%" x2="100%" y2="100%">
    <stop offset="0%" stop-color="#f6dd8b"/>
    <stop offset="45%" stop-color="#d4af37"/>
    <stop offset="100%" stop-color="#9e7514"/>
  </linearGradient>
  <linearGradient id="goldFill" x1="0%" y1="0%" x2="0%" y2="100%">
    <stop offset="0%" stop-color="#f0d47a"/>
    <stop offset="100%" stop-color="#c9971d"/>
  </linearGradient>
  <linearGradient id="cardBg" x1="0%" y1="0%" x2="30%" y2="100%">
    <stop offset="0%" stop-color="#1e1a12"/>
    <stop offset="100%" stop-color="#0c0a06"/>
  </linearGradient>
</defs>"""

SANS = "Manrope, sans-serif"
SERIF = "Cinzel, serif"

def svg_open(w, h):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" font-family="{SANS}">')

# ---------------------------------------------------------------- Баннер 1: Magic Market
def banner_mm():
    W, H = 1600, 900
    s = [svg_open(W, H), DEFS]
    # фон
    s.append('<g id="Background">')
    s.append(f'<rect width="{W}" height="{H}" fill="#050505"/>')
    s.append('<ellipse id="Orb gold" cx="800" cy="75" rx="575" ry="575" fill="url(#orbGold)"/>')
    s.append('<ellipse id="Orb amber" cx="215" cy="905" rx="475" ry="475" fill="url(#orbAmber)"/>')
    s.append('</g>')
    # герой по центру
    s.append('<g id="Hero">')
    s.append(f'<image id="Logo" href="{LOGO}" x="751" y="84" width="98" height="112" '
             f'preserveAspectRatio="xMidYMid meet"/>')
    s.append(f'<text id="Wordmark" x="800" y="252" text-anchor="middle" font-family="{SERIF}" '
             f'font-weight="800" font-size="60" letter-spacing="6">'
             f'<tspan fill="#f59e2d">Magic</tspan><tspan fill="#ffffff"> Market</tspan></text>')
    s.append(f'<text id="Tagline" x="800" y="300" text-anchor="middle" font-size="26" '
             f'font-weight="500" fill="#b7ae98">Премиальный магазин и e-growing в одном Telegram-приложении</text>')
    s.append('</g>')
    # карточки
    cards = [
        ("⚖️", "Товары на вес", [[("Чем больше — тем", 0)], [("дешевле грамм", 1)]]),
        ("🌱", "E-growing", [[("Доли кустов ·", 0)], [("до +35%", 1), (" за цикл", 0)]]),
        ("📦", "Новая Почта", [[("Отправка в день", 1)], [("заказа", 1)], [("трекинг в реальном", 0)], [("времени", 0)]]),
        ("🤝", "Рефералка", [[("До 10%", 1), (" с покупок", 0)], [("друзей", 0)]]),
    ]
    cw, ch, gap, x0, cy = 334, 340, 24, 96, 360
    s.append('<g id="Cards">')
    for i, (ico, title, lines) in enumerate(cards):
        cx = x0 + i * (cw + gap)
        tx = cx + 34
        s.append(f'<g id="Card — {esc(title)}">')
        s.append(f'<rect id="Shell" x="{cx}" y="{cy}" width="{cw}" height="{ch}" rx="30" '
                 f'fill="#ffffff" fill-opacity="0.045" stroke="#d4af37" stroke-opacity="0.16"/>')
        s.append(f'<rect id="Core" x="{cx+8}" y="{cy+8}" width="{cw-16}" height="{ch-16}" rx="23" fill="url(#cardBg)"/>')
        s.append(f'<text id="Icon" x="{tx}" y="502" font-size="52">{ico}</text>')
        s.append(f'<text id="Title" x="{tx}" y="558" font-size="29" font-weight="800" fill="#f4ecd8">{esc(title)}</text>')
        ty = 592
        for line in lines:
            spans = "".join(
                f'<tspan fill="{"#e8c96a" if b else "#9a917c"}" font-weight="{800 if b else 500}">{esc(t)}</tspan>'
                for t, b in line)
            s.append(f'<text id="Subtitle" x="{tx}" y="{ty}" font-size="20">{spans}</text>')
            ty += 27
        s.append('</g>')
    s.append('</g>')
    # футер
    s.append('<g id="Footer">')
    s.append(f'<text id="Handle" x="800" y="792" text-anchor="middle" font-size="34" '
             f'font-weight="800" fill="url(#gold)">@Magic_Marketplace_bot</text>')
    s.append(f'<text id="Payments" x="800" y="828" text-anchor="middle" font-size="19" '
             f'font-weight="600" letter-spacing="3" fill="#8d846f">КАРТА · КРИПТА · USDT · BTC</text>')
    s.append('</g>')
    s.append('</svg>')
    return "\n".join(s)

# ---------------------------------------------------------------- Баннер 2: E-growing
def banner_egrow():
    W, H = 1600, 900
    s = [svg_open(W, H), DEFS]
    s.append('<g id="Background">')
    s.append(f'<rect width="{W}" height="{H}" fill="#050505"/>')
    s.append('<ellipse id="Orb gold" cx="1280" cy="115" rx="575" ry="575" fill="url(#orbGold)"/>')
    s.append('<ellipse id="Orb rose" cx="180" cy="910" rx="490" ry="490" fill="url(#orbRose)"/>')
    s.append('</g>')
    # левый блок
    s.append('<g id="Left">')
    s.append(f'<g id="Logo row"><image href="{LOGO}" x="104" y="66" width="78" height="90" '
             f'preserveAspectRatio="xMidYMid meet"/>'
             f'<text x="200" y="128" font-family="{SERIF}" font-weight="700" font-size="30" letter-spacing="6">'
             f'<tspan fill="#f59e2d">MAGIC</tspan><tspan fill="#f3eee2"> MARKET</tspan></text></g>')
    s.append(f'<text id="Headline" x="104" y="298" font-family="{SERIF}" font-weight="800" '
             f'font-size="132" fill="url(#goldBig)">E-GROWING</text>')
    s.append(f'<text id="Tagline 1" x="104" y="362" font-size="31" font-weight="500" fill="#b7ae98">'
             f'Купи долю куста от 1 000 ₴ — и получи выплату</text>')
    s.append(f'<text id="Tagline 2" x="104" y="404" font-size="31" font-weight="500" fill="#b7ae98">'
             f'после сбора урожая</text>')
    # стадии
    stages = "🌰   →   🌱   →   🌿   →   🌸   →   🌷   →   🧺"
    s.append(f'<text id="Stages" x="104" y="486" font-size="46">{stages}</text>')
    # стат-карточки
    s.append('<g id="Stats">')
    stats = [("+35%", "за цикл"), ("~4 мес", "полный цикл"), ("от 10%", "размер доли")]
    sw, sgap, sx0, sy = 322, 16, 104, 540
    for i, (num, lab) in enumerate(stats):
        sx = sx0 + i * (sw + sgap)
        s.append(f'<g id="Stat — {esc(lab)}">')
        s.append(f'<rect x="{sx}" y="{sy}" width="{sw}" height="110" rx="22" '
                 f'fill="#ffffff" fill-opacity="0.04" stroke="#d4af37" stroke-opacity="0.16"/>')
        s.append(f'<text x="{sx+26}" y="{sy+52}" font-family="{SERIF}" font-weight="800" '
                 f'font-size="34" fill="#f4ecd8">{esc(num)}</text>')
        s.append(f'<text x="{sx+26}" y="{sy+84}" font-size="17" font-weight="600" '
                 f'letter-spacing="0.5" fill="#8d846f">{esc(lab)}</text>')
        s.append('</g>')
    s.append('</g>')
    # кнопка + хэндл
    s.append('<g id="CTA">')
    s.append(f'<rect x="104" y="700" width="284" height="66" rx="33" fill="url(#goldFill)"/>')
    s.append(f'<text x="246" y="742" text-anchor="middle" font-size="30" font-weight="800" '
             f'fill="#140f03">до +35% за цикл</text>')
    s.append(f'<text x="418" y="742" font-size="26" font-weight="800" fill="#e8c96a">@Magic_Marketplace_bot</text>')
    s.append('</g>')
    s.append('</g>')
    # правый блок
    s.append('<g id="Right">')
    s.append(f'<text id="Rose" x="1318" y="470" text-anchor="middle" font-size="290">🌹</text>')
    s.append(f'<text id="ROI" x="1318" y="628" text-anchor="middle" font-family="{SERIF}" '
             f'font-weight="800" font-size="86" fill="#e8c96a">+35%</text>')
    s.append(f'<text id="ROI label" x="1318" y="672" text-anchor="middle" font-size="20" '
             f'font-weight="600" letter-spacing="3" fill="#8d846f">МАКСИМУМ · ВХОД НА СЕМЕЧКЕ</text>')
    s.append('</g>')
    s.append('</svg>')
    return "\n".join(s)

for name, gen in [("banner_mm.svg", banner_mm), ("banner_egrow.svg", banner_egrow)]:
    p = os.path.join(OUT, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(gen())
    print("wrote", p)
