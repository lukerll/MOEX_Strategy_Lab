/* Signal: маскот агента. Векторный SVG, три состояния: idle, thinking, success.
   Использование:
     const s = Signal.create(document.getElementById("slot"), { size: 200, state: "idle" });
     s.setState("thinking");   // расчёт
     s.setState("success");    // прыжок и вспышка кольца, через 1,8 с сам вернётся в idle
   Уважает prefers-reduced-motion: движения выключаются, смена выражения лица остаётся. */
(function (global) {
  "use strict";

  const CSS = `
  .sg { display: inline-block; line-height: 0; }
  .sg svg { width: 100%; height: 100%; overflow: visible; }
  .sg .sg-float { transform-origin: 100px 110px; animation: sg-bob 4.8s ease-in-out infinite; }
  .sg .sg-eyes { transform-origin: 110px 104px; animation: sg-blink 5.6s ease-in-out infinite; }
  .sg .sg-eye { transition: opacity .35s ease, transform .35s ease; transform-box: fill-box; transform-origin: center; }
  .sg .sg-orbit { animation: sg-orbit var(--sg-orbit, 7s) linear infinite; }
  .sg .sg-ring-glow { transition: opacity .5s ease; opacity: .35; }
  .sg .sg-ball { transform-box: fill-box; transform-origin: center; }
  .sg .sg-ball-glow { opacity: .55; transition: opacity .4s ease; }

  /* выражения лица */
  .sg .e-smile { opacity: 1; }
  .sg .e-oval, .sg .e-joy { opacity: 0; transform: scale(.6); }
  .sg[data-state="thinking"] .e-smile, .sg[data-state="success"] .e-smile { opacity: 0; transform: scale(.6); }
  .sg[data-state="thinking"] .e-oval { opacity: 1; transform: none; }
  .sg[data-state="success"] .e-joy { opacity: 1; transform: none; }

  /* thinking: кольцо быстрее, антенна пульсирует, глаза чуть «смотрят» */
  .sg[data-state="thinking"] { --sg-orbit: 2.6s; }
  .sg[data-state="thinking"] .sg-ball { animation: sg-pulse 1.1s ease-in-out infinite; }
  .sg[data-state="thinking"] .sg-ball-glow { animation: sg-glow 1.1s ease-in-out infinite; }
  .sg[data-state="thinking"] .sg-eyes { animation: sg-look 2.2s ease-in-out infinite; }

  /* success: прыжок и вспышка кольца */
  .sg[data-state="success"] .sg-float { animation: sg-jump 1.1s cubic-bezier(.3,.7,.4,1) 1, sg-bob 4.8s ease-in-out 1.1s infinite; }
  .sg[data-state="success"] .sg-ring-glow { opacity: 1; }
  .sg[data-state="success"] .sg-ball-glow { opacity: 1; }

  @keyframes sg-bob { 0%, 100% { transform: translateY(0) rotate(0deg); } 50% { transform: translateY(-4px) rotate(-1.2deg); } }
  @keyframes sg-blink { 0%, 93%, 100% { transform: scaleY(1); } 95.5% { transform: scaleY(.08); } }
  @keyframes sg-look { 0%, 100% { transform: translateX(0); } 30% { transform: translateX(-3px); } 65% { transform: translateX(3px); } }
  @keyframes sg-orbit { to { stroke-dashoffset: -400; } }
  @keyframes sg-pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.25); } }
  @keyframes sg-glow { 0%, 100% { opacity: .45; } 50% { opacity: 1; } }
  @keyframes sg-jump { 0% { transform: translateY(0); } 30% { transform: translateY(-16px) rotate(-3deg); }
    55% { transform: translateY(0) scale(1.03, .97); } 75% { transform: translateY(-4px); } 100% { transform: translateY(0); } }

  @media (prefers-reduced-motion: reduce) {
    .sg .sg-float, .sg .sg-eyes, .sg .sg-orbit, .sg .sg-ball, .sg .sg-ball-glow,
    .sg[data-state] .sg-float, .sg[data-state] .sg-eyes, .sg[data-state] .sg-ball, .sg[data-state] .sg-ball-glow { animation: none !important; }
  }`;

  let injected = false, counter = 0;
  function injectCss() {
    if (injected) return;
    const st = document.createElement("style");
    st.textContent = CSS;
    document.head.appendChild(st);
    injected = true;
  }

  // Одна модель для всех состояний: меняются только глаза, скорость кольца и свечение.
  function svg(id) {
    const u = (n) => `${id}-${n}`;
    return `
<svg viewBox="0 0 220 200" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Signal, агент для проверки стратегий">
  <defs>
    <radialGradient id="${u("shell")}" cx="38%" cy="28%" r="80%">
      <stop offset="0" stop-color="#2f6b52"/><stop offset=".35" stop-color="#143a2b"/>
      <stop offset=".75" stop-color="#0a1a14"/><stop offset="1" stop-color="#030c08"/>
    </radialGradient>
    <linearGradient id="${u("rim")}" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#e3ffef" stop-opacity=".55"/><stop offset=".45" stop-color="#7affb4" stop-opacity=".12"/>
      <stop offset="1" stop-color="#7affb4" stop-opacity=".5"/>
    </linearGradient>
    <radialGradient id="${u("glass")}" cx="40%" cy="30%" r="85%">
      <stop offset="0" stop-color="#14231d"/><stop offset=".55" stop-color="#050b08"/><stop offset="1" stop-color="#000"/>
    </radialGradient>
    <linearGradient id="${u("gloss")}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#e3ffef" stop-opacity=".55"/><stop offset="1" stop-color="#e3ffef" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="${u("ear")}" cx="35%" cy="35%" r="70%">
      <stop offset="0" stop-color="#2b5c47"/><stop offset=".7" stop-color="#0c2219"/><stop offset="1" stop-color="#040d09"/>
    </radialGradient>
    <radialGradient id="${u("ball")}" cx="35%" cy="30%" r="70%">
      <stop offset="0" stop-color="#ffffff"/><stop offset=".35" stop-color="#e3ffef"/><stop offset="1" stop-color="#7affb4"/>
    </radialGradient>
    <radialGradient id="${u("bloom")}" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#7affb4" stop-opacity=".22"/><stop offset=".6" stop-color="#7affb4" stop-opacity=".05"/>
      <stop offset="1" stop-color="#7affb4" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="${u("ring")}" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#7affb4" stop-opacity=".05"/><stop offset=".5" stop-color="#e3ffef" stop-opacity=".75"/>
      <stop offset="1" stop-color="#7affb4" stop-opacity=".05"/>
    </linearGradient>
    <filter id="${u("glow")}" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="2.4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <filter id="${u("soft")}" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="5"/></filter>
    <clipPath id="${u("front")}"><rect x="0" y="108" width="220" height="100"/></clipPath>
    <clipPath id="${u("back")}"><rect x="0" y="0" width="220" height="108"/></clipPath>
    <clipPath id="${u("plate")}"><ellipse cx="118" cy="104" rx="47" ry="34"/></clipPath>
  </defs>

  <!-- мягкое свечение под персонажем -->
  <ellipse cx="110" cy="112" rx="96" ry="78" fill="url(#${u("bloom")})"/>

  <g class="sg-float">
    <!-- кольцо: задняя половина за корпусом -->
    <g transform="rotate(-18 110 110)" clip-path="url(#${u("back")})">
      <ellipse cx="110" cy="110" rx="100" ry="30" fill="none" stroke="url(#${u("ring")})" stroke-width="1.4" opacity=".55"/>
    </g>

    <!-- антенна -->
    <path d="M112 46 C 113 34, 121 27, 127 22" fill="none" stroke="#1d4636" stroke-width="4" stroke-linecap="round"/>
    <path d="M112 46 C 113 34, 121 27, 127 22" fill="none" stroke="#7affb4" stroke-opacity=".35" stroke-width="1.2" stroke-linecap="round"/>
    <circle class="sg-ball-glow" cx="128" cy="19" r="11" fill="#7affb4" filter="url(#${u("soft")})"/>
    <circle class="sg-ball" cx="128" cy="19" r="6.5" fill="url(#${u("ball")})"/>

    <!-- корпус-голова -->
    <ellipse cx="110" cy="104" rx="72" ry="58" fill="url(#${u("shell")})"/>
    <ellipse cx="110" cy="104" rx="72" ry="58" fill="none" stroke="url(#${u("rim")})" stroke-width="1.6"/>
    <path d="M58 82 C 72 54, 118 40, 150 54" fill="none" stroke="#e3ffef" stroke-opacity=".28" stroke-width="5" stroke-linecap="round"/>

    <!-- «наушники» -->
    <g>
      <ellipse cx="44" cy="106" rx="13" ry="20" fill="url(#${u("ear")})" stroke="#7affb4" stroke-opacity=".45" stroke-width="1.2"/>
      <ellipse cx="44" cy="106" rx="7" ry="12" fill="none" stroke="#7affb4" stroke-width="2.4" opacity=".85" filter="url(#${u("glow")})"/>
      <ellipse cx="180" cy="104" rx="7" ry="14" fill="url(#${u("ear")})" stroke="#7affb4" stroke-opacity=".3" stroke-width="1"/>
    </g>

    <!-- стеклянная лицевая панель -->
    <ellipse cx="118" cy="104" rx="47" ry="34" fill="url(#${u("glass")})"/>
    <ellipse cx="118" cy="104" rx="47" ry="34" fill="none" stroke="#7affb4" stroke-opacity=".22" stroke-width="1"/>
    <g clip-path="url(#${u("plate")})">
      <ellipse cx="104" cy="80" rx="40" ry="11" fill="url(#${u("gloss")})" opacity=".35"/>
    </g>

    <!-- глаза: три выражения одной модели -->
    <g class="sg-eyes" filter="url(#${u("glow")})" fill="none" stroke="#7affb4" stroke-linecap="round">
      <g class="sg-eye e-smile" stroke-width="4.2">
        <path d="M94 108 Q 102 97, 110 108"/><path d="M126 108 Q 134 97, 142 108"/>
      </g>
      <g class="sg-eye e-oval" fill="#7affb4" stroke="none">
        <ellipse cx="103" cy="104" rx="4.6" ry="8"/><ellipse cx="133" cy="104" rx="4.6" ry="8"/>
      </g>
      <g class="sg-eye e-joy" stroke-width="4.6">
        <path d="M92 108 Q 102 92, 112 108"/><path d="M124 108 Q 134 92, 144 108"/>
      </g>
    </g>

    <!-- кольцо: передняя половина поверх корпуса, по ней бежит светящийся штрих -->
    <g transform="rotate(-18 110 110)" clip-path="url(#${u("front")})">
      <ellipse cx="110" cy="110" rx="100" ry="30" fill="none" stroke="url(#${u("ring")})" stroke-width="1.6"/>
      <ellipse class="sg-orbit" cx="110" cy="110" rx="100" ry="30" fill="none" stroke="#e3ffef" stroke-width="2.2"
        stroke-linecap="round" stroke-dasharray="26 374" filter="url(#${u("glow")})" opacity=".85"/>
      <ellipse class="sg-ring-glow" cx="110" cy="110" rx="100" ry="30" fill="none" stroke="#7affb4" stroke-width="5"
        filter="url(#${u("soft")})"/>
    </g>
  </g>
</svg>`;
  }

  function create(container, opts = {}) {
    injectCss();
    const id = "sg" + (++counter);
    const size = opts.size || 160;
    const el = document.createElement("span");
    el.className = "sg";
    el.style.width = size + "px";
    el.style.height = Math.round(size * 200 / 220) + "px";
    el.innerHTML = svg(id);
    container.appendChild(el);
    let timer = null;
    const api = {
      el,
      get state() { return el.dataset.state; },
      setState(s) {
        if (!["idle", "thinking", "success"].includes(s)) s = "idle";
        clearTimeout(timer);
        el.dataset.state = s;
        if (s === "success") timer = setTimeout(() => (el.dataset.state = "idle"), opts.successMs || 1800);
        return api;
      },
    };
    api.setState(opts.state || "idle");
    return api;
  }

  global.Signal = { create };
})(window);
