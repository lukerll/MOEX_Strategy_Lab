/* MOEX Strategy Agent: один диалог, аналитика как продолжение ответа Signal.
   Постепенное раскрытие: итог и капитал сразу, остальное в «Подробной аналитике».
   Этапы работы приходят с сервера по мере реального начала, ничего не имитируется. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;
  const isMobile = () => window.matchMedia("(max-width: 760px)").matches;
  const state = { sessionId: localStorage.getItem("msa_session") || null, runs: new Map(), pending: new Set(),
                  lastTrace: null, busy: false, started: false, list: "hist", hist: [], favs: [], active: null, gen: 0 };
  const prefs = { steps: true, style: localStorage.getItem("msa_style") === "short" ? "short" : "full",
    signal: localStorage.getItem("msa_signal") !== "0", simple: localStorage.getItem("msa_simple") === "1", cash: +localStorage.getItem("msa_cash") || null };

  // ---------- форматирование ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (v, d = 2) => { if (v == null || !isFinite(v)) return "н/д"; let n = Number(v); if (Math.abs(n) < 0.5 * 10 ** -d) n = 0;
    return n.toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d }).replace(/\s/g, "\u00a0").replace("-", "−"); };
  const pct = (v, sign = true, d = 2) => v == null ? "н/д" : (sign && v > 0.004 ? "+" : "") + fmt(v, d) + "%";
  const rub = (v, sign = false) => (v < 0 ? "−" : sign && v > 0 ? "+" : "") + Math.round(Math.abs(v)).toLocaleString("ru-RU").replace(/[\s,]/g, "\u00a0") + "\u00a0₽";
  const tone = (v) => v == null ? "" : v < -0.004 ? "neg" : v > 0.004 ? "pos" : "";
  const negc = (v) => v != null && v < -0.004 ? "neg" : "";
  // оговорка стоит один раз внизу отчёта, в тексте ответа её не повторяем
  const noDisc = (t) => String(t || "").replace(/\s*Не является индивидуальной инвестиционной рекомендацией\.?/g, "").trim();
  document.documentElement.classList.toggle("simple", prefs.simple);
  const dmy = (d) => d ? String(d).slice(0, 10).split("-").reverse().join(".") : "";
  const paras = (t) => String(t).split(/\n{2,}/).map((p) => `<p>${esc(p).replace(/\n/g, "<br>")}</p>`).join("");

  // ---------- термины ----------
  const TERMS = {
    hold: "Сравнение по умолчанию: купить акцию в первый день периода и ничего не делать до конца. Если стратегия его не обгоняет, сложность не окупилась.",
    dd: "Насколько капитал опускался от своего пика до дна. Это то, что пришлось бы пережить, держа стратегию.",
    pp: "Процентные пункты: разница между двумя процентами. 10% против 4% это 6 п.п.",
    trades: "Сколько раз стратегия купила акцию, включая позицию, которая ещё не продана к концу периода.",
    vs: "Разница итогового капитала стратегии и простого держания акции, в рублях.",
    verify: "Результат считают два независимых движка. Если цифры совпали, ошибке в расчёте взяться неоткуда.",
    synth: "Демонстрационные цены, похожие на биржевые. Для настоящих котировок сервис запускают без флага --demo.",
    unstable: "Проверяем 8 соседних настроек. Если меньше двух третей из них тоже обгоняют держание, выигрыш похож на подгонку под историю.",
    SMA: "Простая скользящая средняя: средняя цена за последние N дней.",
    EMA: "Экспоненциальная средняя: как простая, но свежие дни весят больше.",
    RSI: "Индекс относительной силы от 0 до 100. Ниже 30 акцию считают перепроданной, выше 70 перекупленной.",
    breakout: "Покупка, когда цена закрывается выше максимума за N дней, продажа, когда ниже минимума за M дней.",
    cagr: "Среднегодовая доходность с учётом сложного процента.",
    sharpe: "Доходность на единицу колебаний. Выше 1 считается хорошо, ниже 0 стратегия проигрывает деньгам без движения.",
    exposure: "Какую часть периода деньги были вложены в акцию.",
    win: "Доля закрытых сделок, которые закончились в плюс.",
    equity: "Сколько стоил бы портфель каждый день: деньги плюс акции по цене закрытия. Пунктир: если просто купить акцию в первый день и держать. Тонкая линия: начальный капитал.",
  };
  const term = (label, key) => `<span class="term" tabindex="0" data-tip="${esc(TERMS[key])}">${label}</span>`;
  const withTerms = (html) => html.replace(/(простого держания акции)/, term("$1", "hold")).replace(/п\.п\./, term("п.п.", "pp"))
    .replace(/(Самое глубокое падение)/, term("$1", "dd")).replace(/(неустойчив к параметрам)/, term("$1", "unstable"));

  const NAMES = { SBER: "Сбербанк", GAZP: "Газпром", LKOH: "Лукойл", YDEX: "Яндекс", VTBR: "ВТБ", ROSN: "Роснефть",
    GMKN: "Норникель", NVTK: "Новатэк", MGNT: "Магнит", TATN: "Татнефть", MTSS: "МТС", MOEX: "Мосбиржа" };
  const NAMES_GEN = { SBER: "Сбербанка", GAZP: "Газпрома", LKOH: "Лукойла", YDEX: "Яндекса", VTBR: "ВТБ", ROSN: "Роснефти",
    GMKN: "Норникеля", NVTK: "Новатэка", MGNT: "Магнита", TATN: "Татнефти", MTSS: "МТС", MOEX: "Мосбиржи" };
  const company = (t) => NAMES[t] || t;

  function strategyText(spec, withTerm = false) {
    const p = spec.parameters || {}, T = (l, k) => withTerm ? term(l, k) : l;
    switch (spec.strategy_type) {
      case "SMA_CROSSOVER": return p.fast_period === 1 ? `цена против ${T("средней", "SMA")} за ${p.slow_period} дней`
        : `${T("скользящие средние", "SMA")} ${p.fast_period} и ${p.slow_period} дней`;
      case "EMA_CROSSOVER": return p.fast_period === 1 ? `цена против ${T("экспоненциальной средней", "EMA")} за ${p.slow_period} дней`
        : `${T("экспоненциальные средние", "EMA")} ${p.fast_period} и ${p.slow_period} дней`;
      case "RSI": return `${T("RSI", "RSI")} ${p.period}: покупка ниже ${p.lower}, продажа выше ${p.upper}`;
      case "BREAKOUT": return `${T("пробой", "breakout")} ${p.entry_lookback}-дневного максимума, выход по минимуму за ${p.exit_lookback} дней`;
      case "RULES": return "свои правила";
      default: return "купить и держать";
    }
  }
  const shortStrategy = (h) => (h.strategy || "").split(": ").slice(1).join(": ") || "стратегия";
  const yr = (d) => String(d).slice(0, 4);
  const period = (s) => yr(s.start_date) === yr(s.end_date) ? `${yr(s.start_date)} год` : `${yr(s.start_date)}-${yr(s.end_date)}`;

  // ---------- вердикт и предупреждения: только из результата расчёта ----------
  function verdict(r) {
    const m = r.metrics, b = r.benchmark, cash = r.spec.initial_cash;
    const dd = Math.abs(m.max_drawdown_pct), bdd = Math.abs(b.max_drawdown_pct);
    let main, risk;
    if (!r.trades.length) {
      main = `Стратегия ни разу не купила акцию: ${rub(cash)} так и остались ${rub(cash)}.`;
      risk = `Если ${term("просто держать акцию", "hold")}, было бы ${rub(b.final_equity)}.`;
    } else {
      main = `Из ${rub(cash)} стало ${rub(m.final_equity)}, а если ${term("просто держать акцию", "hold")}, было бы ${rub(b.final_equity)}.`;
      const word = dd < bdd * 0.8 ? "Падала заметно мельче" : dd > bdd * 1.2 ? "Падала глубже" : "Падала примерно так же";
      risk = `${word}: ${term("худшее падение", "dd")} ${fmt(dd)}% против ${fmt(bdd)}% у держания.`;
    }
    const warns = [];
    if (r.verification.status === "FAIL") warns.push(["strong", `Два независимых расчёта не совпали (${term("сверка", "verify")}), этим цифрам доверять нельзя`]);
    if ((r.diagnostics.findings || []).some((f) => f.includes("неустойчив"))) warns.push(["", `Результат ${term("неустойчив к параметрам", "unstable")}`]);
    if (r.data.is_synthetic) warns.push(["", `Котировки ${term("демонстрационные", "synth")}, не биржевые`]);
    if (r.data.source === "cache") warns.push(["", `Биржа была недоступна, котировки из кеша до ${dmy(r.data.last_date)}`]);
    const a = r.assumptions || [];
    const per = a.find((x) => x.startsWith("период не указан")), cap = a.find((x) => x.startsWith("капитал не указан"));
    if (per || cap) warns.push(["", `${per && cap ? "Период и капитал" : per ? "Период" : "Капитал"} подставлен по умолчанию`]);
    return { main, risk, warns, line: verdictLine(r) };
  }
  // одна фраза над метриками: что получилось по деньгам и по риску, без чисел, которые и так ниже
  function verdictLine(r) {
    const m = r.metrics, b = r.benchmark, diff = m.final_equity - b.final_equity;
    if (r.spec.strategy_type === "BUY_AND_HOLD") return "Это и есть держание: точка отсчёта, с которой сравнивают остальные стратегии.";
    if (!r.trades.length) return "Сигналов не было: стратегия весь период просидела в деньгах.";
    const dd = Math.abs(m.max_drawdown_pct), bdd = Math.abs(b.max_drawdown_pct);
    const tol = r.spec.initial_cash * 0.005, up = m.total_return_pct > 0;
    if (Math.abs(diff) < tol && Math.abs(dd - bdd) <= bdd * 0.2) return "И результат, и просадка почти как у держания: стратегия мало что меняет.";
    const money = Math.abs(diff) < tol ? "Результат почти как у держания"
      : diff > 0 ? (up ? `Заработала больше держания на ${rub(diff)}` : `Потеряла меньше держания на ${rub(diff)}`)
      : (up ? `В плюсе, но держание дало бы на ${rub(-diff)} больше` : `Отстала от держания на ${rub(-diff)}`);
    const goodRisk = dd < bdd * 0.8, badRisk = dd > bdd * 1.2;
    const risk = goodRisk ? "падала заметно мельче" : badRisk ? "падала глубже" : "падала примерно так же";
    const goodMoney = diff > tol;
    const join = (goodMoney && !badRisk) || (!goodMoney && !goodRisk) ? (money.includes(", но ") ? ", и " : " и ") : (money.includes(", но ") ? ", зато " : ", но ");
    return `${money}${join}${risk}.`;
  }

  // ---------- сеть ----------
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!r.ok) { let d = r.statusText; try { const j = await r.json(); d = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) {} const e = new Error(d); e.status = r.status; throw e; }
    return r.json();
  }
  const getRun = async (id) => { if (!state.runs.has(id)) state.runs.set(id, await api(`/api/runs/${id}`)); return state.runs.get(id); };
  function toast(t) {
    const el = $("toast");
    el.textContent = String(t || "").replace(/\.$/, "");        // короткая реплика без точки в конце
    el.hidden = false; requestAnimationFrame(() => el.classList.add("show"));
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.classList.remove("show"); setTimeout(() => (el.hidden = true), 200); }, 2200);
  }
  const sid = () => {
    if (!state.sessionId) { state.sessionId = "ui" + Math.random().toString(36).slice(2, 12); localStorage.setItem("msa_session", state.sessionId); }
    return state.sessionId;
  };

  // ---------- подтверждение человеком ----------
  function askConfirm(text, yes = "Да", no = "Нет") {
    return new Promise((resolve) => {
      const m = $("modal"), prev = document.activeElement;
      $("modalText").textContent = text; $("modalYes").textContent = yes; $("modalNo").textContent = no; m.hidden = false; $("modalYes").focus();
      const done = (v) => { m.hidden = true; $("modalYes").onclick = $("modalNo").onclick = null; document.removeEventListener("keydown", key, true); if (prev) prev.focus(); resolve(v); };
      const key = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
      $("modalYes").onclick = () => done(true); $("modalNo").onclick = () => done(false);
      document.addEventListener("keydown", key, true);
    });
  }
  async function humanAction(req) {
    const p = await req();
    const ok = await askConfirm(p.description + "?");
    const r = await api(`/api/actions/${p.action_id}/${ok ? "confirm" : "reject"}`, { method: "POST" });
    toast(r.text); return ok && r.ok;
  }

  // ---------- Plotly: локаль и цвета темы ----------
  Plotly.register({ moduleType: "locale", name: "ru", dictionary: {}, format: {
    days: ["воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота"], shortDays: ["вс", "пн", "вт", "ср", "чт", "пт", "сб"],
    months: ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"],
    shortMonths: ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"],
    date: "%d.%m.%Y", decimal: ",", thousands: " " } });
  const C = {};
  let BASE, AX, XAX;
  function readTheme() {
    const cs = getComputedStyle(root), v = (n) => cs.getPropertyValue(n).trim();
    Object.assign(C, { text: v("--text"), muted: v("--muted"), mint: v("--ch-strategy"), bench: v("--ch-bench"), neg: v("--neg"), negSoft: v("--neg-soft"), osc: v("--ch-osc"),
      grid: v("--ch-grid"), start: v("--ch-start"), bg: v("--bg"), elev: v("--elev"), line: v("--line-strong"), price: v("--ch-price"),
      ov: [v("--ch-ov1"), v("--ch-ov2"), v("--ch-osc")], level: v("--ch-level"), hl: v("--ch-hl"), vol: v("--ch-vol") });
    BASE = { paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", separators: ", ", showlegend: false,
      font: { family: "Inter, system-ui, sans-serif", size: 11, color: C.muted }, margin: { l: 66, r: 8, t: 6, b: 24 },
      hovermode: "x unified", dragmode: isMobile() ? false : "zoom",
      hoverlabel: { bgcolor: C.elev, bordercolor: C.line, font: { color: C.text, family: "Inter, sans-serif", size: 12 } } };
    AX = { gridcolor: C.grid, zeroline: false, linecolor: "rgba(0,0,0,0)", tickfont: { color: C.muted }, showspikes: false, fixedrange: isMobile() };
    XAX = { ...AX, type: "date", hoverformat: "%d.%m.%Y", showgrid: false, nticks: 6,
      tickformatstops: [{ dtickrange: [null, 864e5 * 40], value: "%d %b" }, { dtickrange: [864e5 * 40, null], value: "%b %Y" }] };
  }
  readTheme();
  const CFG = { displayModeBar: false, responsive: true, locale: "ru", doubleClick: "reset", scrollZoom: false };

  // =================== боковая панель ===================
  function syncToggle() {
    const rail = root.classList.contains("rail"), open = root.classList.contains("side-open");
    const expanded = isMobile() ? open : !rail;
    const b = $("sideToggle");
    b.setAttribute("aria-expanded", String(expanded));
    const label = isMobile() ? (open ? "Закрыть меню" : "Открыть меню") : (rail ? "Развернуть панель" : "Свернуть панель");
    b.setAttribute("aria-label", label); b.dataset.tip = label;
  }
  function setRail(on, remember = true) {
    root.classList.toggle("rail", on);
    if (remember) { state.railPeek = false; try { localStorage.setItem("msa_rail", on ? "1" : "0"); } catch (_) {} }
    syncToggle();
    setTimeout(resizePlots, 220);                                  // после анимации колонки
  }
  function toggleSideMobile(open) {
    const on = open ?? !root.classList.contains("side-open");
    root.classList.toggle("side-open", on); $("sideScrim").hidden = !on; syncToggle();
    if (on) $("newBtn").focus();
  }
  $("sideToggle").addEventListener("click", () => isMobile() ? toggleSideMobile() : setRail(!root.classList.contains("rail")));
  $("sideScrim").addEventListener("click", () => toggleSideMobile(false));
  const closeMobileSide = () => { if (isMobile()) toggleSideMobile(false); };

  async function loadSide() {
    try {
      const [h, f] = await Promise.all([api("/api/runs?limit=40"), api("/api/research")]);
      state.hist = h.runs; state.favs = f.research;
    } catch (e) { $("sideList").innerHTML = `<p class="side-empty">Список недоступен: ${esc(e.message)}</p>`; return; }
    $("histCnt").textContent = state.hist.length; $("favCnt").textContent = state.favs.length;
    renderSide();
  }
  function renderSide() {
    const fav = state.list === "fav", items = fav ? state.favs : state.hist;
    $("listCap").textContent = fav ? "Избранное" : "Последние запросы";
    document.querySelectorAll(".side-btn[data-list]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.list === state.list)));
    if (!items.length) {
      $("sideList").innerHTML = `<p class="side-empty">Пусто</p>`;
      return;
    }
    $("sideList").innerHTML = items.map((x) => `<div class="side-item${x.run_id === state.active ? " active" : ""}" role="button" tabindex="0" data-run="${esc(x.run_id)}" data-sid="${esc(x.session_id || "")}">
        <span class="t">${esc(company(x.ticker || ""))}, ${esc(shortStrategy(x))}</span>
        <span class="s">${x.start_date ? period(x) : ""}<span class="r ${tone(x.total_return_pct)}">${pct(x.total_return_pct, true, 1)}</span></span>
        ${fav ? `<button class="del" type="button" data-del="${esc(x.id)}" aria-label="Убрать из избранного" title="Убрать из избранного"><svg><use href="#i-x"/></svg></button>`
              : `<button class="del" type="button" data-delrun="${esc(x.run_id)}" aria-label="Удалить из истории" title="Удалить из истории"><svg><use href="#i-x"/></svg></button>`}
      </div>`).join("");
  }
  document.querySelectorAll(".side-btn[data-list]").forEach((b) => b.addEventListener("click", () => {
    // в узкой панели список не помещается: раскрываем её на время выбора и потом возвращаем как было
    if (root.classList.contains("rail") && !isMobile()) { setRail(false, false); state.railPeek = true; }
    state.list = b.dataset.list; renderSide();
  }));
  $("sideList").addEventListener("click", async (e) => {
    const delRun = e.target.closest("[data-delrun]"), delFav = e.target.closest("[data-del]");
    if (delRun || delFav) {
      e.stopPropagation();
      try {
        if (delRun) { const id = delRun.dataset.delrun;
          if (await humanAction(() => api(`/api/runs/${id}/delete?session_id=${sid()}`, { method: "POST" }))) { forgetRun(id); loadSide(); } }
        else if (await humanAction(() => api(`/api/research/${delFav.dataset.del}/delete?session_id=${sid()}`, { method: "POST" }))) { loadSide(); refreshStars(); }
      } catch (err) { toast("Не удалось: " + err.message); }
      return;
    }
    const it = e.target.closest(".side-item");
    if (it) { openFromHistory(it.dataset.run, it.dataset.sid); closeMobileSide(); if (state.railPeek) { state.railPeek = false; setRail(true, false); } }
  });
  $("sideList").addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.classList.contains("side-item")) { e.preventDefault(); e.target.click(); }
  });
  function setActive(runId) {
    state.active = runId;
    document.querySelectorAll(".side-item").forEach((i) => i.classList.toggle("active", i.dataset.run === runId));
  }

  // =================== навигация: логотип и «Новый анализ» делают одно и то же ===================
  async function newAnalysis() {
    if (state.busy && !(await askConfirm("Анализ ещё выполняется. Начать новый? Результат текущего сохранится в истории.", "Начать новый", "Подождать"))) return;
    state.gen++;                                                   // ответ прежнего запроса не попадёт в новый экран
    clearThread();
    state.sessionId = null; localStorage.removeItem("msa_session");
    showIntro(true); setChips([]); setActive(null); setBusy(false);
    input.value = ""; autosize(); $("scopeHint").hidden = true; closeMobileSide(); input.focus();
    history.replaceState(null, "", "/");
  }
  $("newBtn").addEventListener("click", newAnalysis);
  $("brand").addEventListener("click", (e) => { e.preventDefault(); newAnalysis(); });

  // клик по истории: открываем тот диалог, где анализ был сделан, чтобы ничего не смешивать
  async function openFromHistory(runId, sessionId) {
    const here = log.querySelector(`.ap[data-run="${runId}"]`);
    if (here) { expandPanel(here, true); here.closest(".msg").scrollIntoView({ block: "start", behavior: "smooth" }); return; }
    state.gen++;
    clearThread(); showIntro(false); setChips([]);
    if (sessionId) {
      state.sessionId = sessionId; localStorage.setItem("msa_session", sessionId);
      const shown = await restore(runId);
      if (shown) return;
    }
    // анализ без диалога (например, запущен через API): показываем отдельно и честно подписываем
    state.sessionId = null; localStorage.removeItem("msa_session");
    const d = agentShell(); d.classList.add("from-hist");
    d.querySelector(".a-body").innerHTML = `<p class="sub">Анализ открыт из истории, диалога к нему нет. Новый вопрос начнёт новый разговор.</p>`;
    await attachResult(d, runId, { text: "" }, { restoring: true });
    feed.scrollTop = 0;
  }

  function clearThread() {
    log.querySelectorAll(".msg").forEach((m) => { m.querySelectorAll(".plot").forEach((p) => Plotly.purge(p)); m.remove(); });
  }
  function showIntro(on) {
    state.started = !on; $("intro").hidden = !on; document.body.classList.toggle("fresh", on);
  }

  // =================== прокрутка как в AI-чатах ===================
  const feed = $("feed"), log = $("chatLog");
  const atBottom = () => feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90;
  const toBottom = (smooth = true) => feed.scrollTo({ top: feed.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  feed.addEventListener("scroll", () => { const b = atBottom(); $("toBottom").hidden = b; if (b) $("toBottom").classList.remove("new"); }, { passive: true });
  $("toBottom").addEventListener("click", () => toBottom());
  // новое содержимое: следуем вниз, только если человек и так был внизу
  function follow(wasBottom, el) {
    if (wasBottom) { if (el) el.scrollIntoView({ block: "start", behavior: "smooth" }); else toBottom(); }
    else { $("toBottom").hidden = false; $("toBottom").classList.add("new"); }
  }

  // =================== сообщения ===================
  function addUser(text) {
    showIntro(false);
    const d = document.createElement("div"); d.className = "msg user";
    d.innerHTML = `<div class="u-bubble">${esc(text)}</div>`;
    log.appendChild(d); return d;
  }
  function agentShell(st = "idle") {
    const d = document.createElement("div"); d.className = "msg agent";
    d.innerHTML = `<div class="a-head"><span class="av"></span><span class="nm">Signal</span></div><div class="a-body"></div>`;
    log.appendChild(d);
    const m = Signal.create(d.querySelector(".av"), { size: 26, state: st }); m.el.classList.add("sg-mini"); d._mascot = m;
    return d;
  }

  const STAGES = { understand: "Анализирую запрос и формализую правила", data: "Получаю котировки",
    backtest: "Выполняю бэктест и независимую проверку", news: "Ищу новости рядом с резкими движениями цены",
    narrate: "Формирую отчёт", answer: "Готовлю ответ" };
  function stepsLive(d) {
    const box = document.createElement("div"); box.className = "steps"; box.innerHTML = "<ol></ol>";
    d.querySelector(".a-body").appendChild(box);
    const list = []; const t0 = performance.now();
    const LLM_STAGES = new Set(["understand", "narrate", "answer"]);
    // этап с моделью идёт слишком долго: честно говорим, что происходит, а не крутим спиннер молча
    const slowNote = (s) => {
      if (s.end || !LLM_STAGES.has(s.name)) return "";
      const sec = (performance.now() - s.start) / 1000;
      if (sec < 12) return "";
      return `<span class="slow">${sec < 22 ? "GigaChat отвечает дольше обычного." : "GigaChat всё ещё молчит. С включённым VPN ответа может не быть вовсе."}</span>`;
    };
    const draw = () => {
      box.querySelector("ol").innerHTML = list.map((s, i) => {
        const last = i === list.length - 1;
        const dt = s.end ? `<span class="dt">${fmt((s.end - s.start) / 1000, 1)} с</span>` : "";
        return `<li class="${last && !s.end ? "now" : ""}"><span class="ic">${s.end ? '<svg><use href="#i-check"/></svg>' : '<span class="spin"></span>'}</span>${esc(s.label)}${dt}${last ? slowNote(s) : ""}</li>`;
      }).join("") || `<li class="now"><span class="ic"><span class="spin"></span></span>Отправляю запрос</li>`;
    };
    draw();
    const tick = setInterval(() => { const s = list[list.length - 1]; if (s && !s.end && LLM_STAGES.has(s.name)) draw(); }, 1000);
    return {
      stage(name) {
        const now = performance.now(); if (list.length) list[list.length - 1].end ??= now;
        list.push({ name, label: STAGES[name] || name, start: now }); draw();
      },
      finish(ok) {
        clearInterval(tick);
        const now = performance.now(); if (list.length) list[list.length - 1].end ??= now;
        if (!list.length || !prefs.steps) { box.remove(); return; }
        const total = fmt((now - t0) / 1000, 1);
        const det = document.createElement("details"); det.className = "steps";
        det.innerHTML = `<summary>Ход анализа: ${list.length} ${list.length === 1 ? "этап" : list.length < 5 ? "этапа" : "этапов"}, ${total} с<svg><use href="#i-chev"/></svg></summary><ol></ol>`;
        det.querySelector("ol").innerHTML = list.map((s) => `<li><span class="ic">${ok || s !== list[list.length - 1] ? '<svg><use href="#i-check"/></svg>' : ""}</span>${esc(s.label)}<span class="dt">${fmt((s.end - s.start) / 1000, 1)} с</span></li>`).join("");
        box.replaceWith(det);
      },
    };
  }

  // ошибки показываются отдельно от обычных ответов, с реальными действиями
  const ERR = {
    data_unavailable: "Не удалось получить котировки", model_unavailable: "Языковая модель недоступна",
    error: "Не получилось выполнить расчёт", internal_error: "Внутренняя ошибка сервиса",
    budget_exceeded: "Остановился по лимиту запроса", network: "Сервис не ответил",
  };
  function errorKind(reply) {
    if (reply.intent === "data_unavailable") return "data_unavailable";
    if (reply.intent === "model_unavailable") return "model_unavailable";
    if ((reply.flags || []).includes("budget_exceeded")) return "budget_exceeded";
    if ((reply.flags || []).includes("internal_error")) return "internal_error";
    if (reply.intent === "error") return "error";
    return null;
  }
  const LLM_TITLES = {
    no_key: "Нет ключа GigaChat", auth: "GigaChat не принял ключ", timeout: "GigaChat не ответил вовремя",
    network: "Нет связи с GigaChat", tls: "Не прошла проверка сертификата", limit: "Лимит запросов GigaChat",
    server: "Сбой на стороне GigaChat",
  };
  const llmKind = (flags) => (flags || []).map((f) => /^llm_(\w+)$/.exec(f)?.[1]).find((k) => k && LLM_TITLES[k]) || null;
  function renderError(d, kind, text, askedText, flags) {
    const sub = kind === "model_unavailable" ? llmKind(flags) : null;
    const prev = [...log.querySelectorAll(".ap")].pop();
    const acts = [`<button class="btn-ghost btn-sm" type="button" data-act="retry">Повторить запрос</button>`];
    if (kind === "data_unavailable" || kind === "budget_exceeded" || kind === "error") acts.push(`<button class="btn-ghost btn-sm" type="button" data-act="edit">Изменить запрос</button>`);
    if (kind === "model_unavailable" && (!sub || ["no_key", "auth", "tls"].includes(sub))) acts.push(`<button class="btn-ghost btn-sm" type="button" data-act="settings">Открыть настройки</button>`);
    if (prev) acts.push(`<button class="btn-ghost btn-sm" type="button" data-act="prev">К предыдущему анализу</button>`);
    d.querySelector(".a-body").insertAdjacentHTML("beforeend", `<div class="err-card" role="alert"><div class="eh"><svg><use href="#i-alert"/></svg>${sub ? LLM_TITLES[sub] : ERR[kind]}</div>
      <div class="et">${esc(text)}</div><div class="ea">${askedText ? acts.join("") : ""}</div></div>`);
    d.querySelector(".err-card").addEventListener("click", (e) => {
      const a = e.target.closest("[data-act]")?.dataset.act; if (!a) return;
      if (a === "retry") send(askedText);
      if (a === "edit") { input.value = askedText; autosize(); input.focus(); }
      if (a === "settings") $("settingsBtn").click();
      if (a === "prev" && prev) { expandPanel(prev, true); prev.closest(".msg").scrollIntoView({ block: "start", behavior: "smooth" }); }
    });
  }

  // ответ агента: текст обычным потоком; у бэктеста отчёт как продолжение ответа
  function renderReply(d, reply, opts = {}) {
    const body = d.querySelector(".a-body");
    const result = (reply.blocks || []).find((b) => b.type === "result" && !b.tab);
    const kind = errorKind(reply);
    if (kind) renderError(d, kind, reply.text, opts.asked, reply.flags);
    else if (result) { d.dataset.run = result.run_id; d._ready = attachResult(d, result.run_id, reply, opts); }
    else body.insertAdjacentHTML("beforeend", paras(reply.text || ""));
    body.insertAdjacentHTML("beforeend", (reply.blocks || []).map((b) => confirmBlock(b, opts.restoring)).join(""));
    if (!opts.restoring) followIntent(reply);
  }

  function confirmBlock(b, restoring) {
    if (b.type !== "confirm") return "";
    const live = !restoring || state.pending.has(b.action_id);
    return `<div class="confirm" data-action="${esc(b.action_id)}"><div>${esc(b.text)}?</div>${live
      ? `<div class="row"><button class="btn-mint btn-sm" type="button" data-confirm="1">Да</button><button class="btn-ghost btn-sm" type="button" data-confirm="0">Нет</button></div>`
      : `<div class="done">Решение уже принято</div>`}</div>`;
  }

  // «покажи сделки» открывает вкладку сделок у текущего отчёта, «почему» и «новости» свои разделы
  function followIntent(reply) {
    const types = (reply.blocks || []).map((b) => b.type + (b.tab ? ":" + b.tab : ""));
    const panel = state.active && log.querySelector(`.ap[data-run="${state.active}"]`);
    if (!panel) return;
    const tab = types.includes("trades") ? "trades" : types.includes("result:weak") ? "findings" : types.includes("news") ? "check" : null;
    if (tab) { expandPanel(panel); openDetail(panel, tab); }
  }

  const worthCelebrating = (r) => r.metrics.final_equity > r.benchmark.final_equity && r.verification.status === "OK" &&
    !(r.diagnostics.findings || []).some((f) => f.includes("неустойчив"));

  async function attachResult(d, runId, reply, opts = {}) {
    const body = d.querySelector(".a-body");
    try {
      const r = await getRun(runId);
      const v = verdict(r), txt = noDisc(reply.text || r.narrative || "");
      const s = r.spec;
      body.insertAdjacentHTML("beforeend", `<p class="lead">Проверил стратегию на истории ${esc(NAMES_GEN[s.ticker] || s.ticker)} за ${period(s)}. ${v.main}</p>
        <p class="sub">${v.risk}</p>
        ${txt ? `<button class="linkish" type="button" aria-expanded="false" data-explain>Объяснение Signal<svg><use href="#i-chev"/></svg></button><div class="full" hidden>${paras(txt)}</div>` : ""}`);
      const ex = body.querySelector("[data-explain]");
      if (ex) ex.addEventListener("click", () => { const f = body.querySelector(".full"); f.hidden = !f.hidden; ex.setAttribute("aria-expanded", String(!f.hidden)); });
      const panel = document.createElement("section"); panel.className = "ap"; panel.dataset.run = r.run_id; panel._r = r; panel._sugg = reply.suggestions || [];
      d.appendChild(panel);
      if (opts.collapsed) collapsePanel(panel); else expandPanel(panel, !opts.restoring);
      if (!opts.restoring && d._mascot && worthCelebrating(r)) d._mascot.setState("success");
      return true;
    } catch (e) {
      body.insertAdjacentHTML("beforeend", e.status === 404 ? `<p class="gone">Этот анализ удалён из истории.</p>`
        : `<p class="gone">Результат недоступен: ${esc(e.message)}</p>`);
      return false;
    }
  }

  function forgetRun(runId) {
    state.runs.delete(runId);
    log.querySelectorAll(`.ap[data-run="${runId}"]`).forEach((p) => {
      p.querySelectorAll(".plot").forEach((x) => Plotly.purge(x));
      const body = p.closest(".msg")?.querySelector(".a-body"); p.remove();
      if (body) body.insertAdjacentHTML("beforeend", `<p class="gone">Анализ удалён из истории.</p>`);
    });
    if (state.active === runId) setActive(null);
  }

  // =================== отправка: потоковые этапы, запасной путь без потока ===================
  function setBusy(on) { state.busy = on; $("sendBtn").disabled = on; $("composer").setAttribute("aria-busy", String(on)); }
  async function send(text) {
    text = (text || "").trim();
    if (state.busy || !text) return;
    const gen = state.gen;
    setBusy(true); setChips([]);
    const u = addUser(text);
    const d = agentShell("thinking");
    const steps = stepsLive(d);
    u.scrollIntoView({ block: "start", behavior: "smooth" });      // видно новый запрос и начало ответа
    let reply = null, failed = null;
    try {
      reply = await streamChat(text, (stage) => { if (gen !== state.gen) return; const wb = atBottom(); steps.stage(stage); if (wb) toBottom(); });
    } catch (e) { failed = e; }
    if (gen !== state.gen) { setBusy(false); return; }            // пользователь ушёл в новый анализ: ответ уже в истории
    d._mascot.setState("idle");
    const wasBottom = atBottom();
    if (reply) {
      state.sessionId = reply.session_id; localStorage.setItem("msa_session", reply.session_id);
      state.lastTrace = reply.trace_id;
      if (reply.pending_action) state.pending.add(reply.pending_action.id);
      steps.finish(!errorKind(reply));
      renderReply(d, reply, { asked: text });
      setChips(reply.suggestions);
      if (reply.run_id) loadSide();
    } else {
      steps.finish(false);
      renderError(d, "network", `${failed ? failed.message : "нет ответа"}. Проверьте, что сервер запущен, и повторите.`, text);
    }
    if (d._ready) await d._ready;
    follow(wasBottom, d);
    setBusy(false); if (!isMobile()) input.focus();
  }

  async function streamChat(text, onStage) {
    const body = JSON.stringify({ message: text, session_id: state.sessionId, style: prefs.style, ...(prefs.cash ? { initial_cash: prefs.cash } : {}) });
    let r;
    try { r = await fetch("/api/chat/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body }); } catch (e) { r = null; }
    if (!r || !r.ok || !r.body) return api("/api/chat", { method: "POST", body });          // запасной путь
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "", reply = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        if (ev.type === "stage") onStage(ev.stage);
        else if (ev.type === "reply") reply = ev.reply;
        else if (ev.type === "error") throw new Error(ev.error);
      }
    }
    if (!reply) throw new Error("ответ оборвался");
    return reply;
  }

  // =================== поле ввода ===================
  const input = $("input");
  const autosize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; };
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); if (state.busy) return; const v = input.value; if (!v.trim()) return; input.value = ""; autosize(); $("scopeHint").hidden = true; send(v); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit ? $("composer").requestSubmit() : $("composer").dispatchEvent(new Event("submit", { cancelable: true })); } });
  input.addEventListener("input", autosize);
  // подсказка «это я не умею» до отправки: тот же scope_check, что и в агенте, без модели и токенов
  let hintTimer = 0, hintSeq = 0;
  const hintEl = $("scopeHint");
  const setHint = (t) => { hintEl.textContent = t || ""; hintEl.hidden = !t; };
  input.addEventListener("input", () => {
    clearTimeout(hintTimer);
    const text = input.value;
    if (text.trim().length < 8) { setHint(""); return; }
    hintTimer = setTimeout(async () => {
      const seq = ++hintSeq;
      try {
        const r = await fetch(`/api/scope?text=${encodeURIComponent(text.slice(0, 1000))}`);
        const js = await r.json();
        if (seq === hintSeq && input.value === text) setHint(js.hint);
      } catch { /* подсказка необязательна */ }
    }, 350);
  });
  function setChips(list) {
    $("chips").innerHTML = (list || []).slice(0, 3).map((s) => `<button class="chip" type="button" data-q="${esc(s)}">${esc(s)}</button>`).join("");
  }
  $("chips").addEventListener("click", (e) => { const q = e.target.closest("[data-q]"); if (q) send(q.dataset.q); });
  // примеры подставляются в поле, анализ запускает человек
  $("examples").addEventListener("click", (e) => {
    const b = e.target.closest(".example"); if (!b) return;
    input.value = b.dataset.q; autosize(); input.focus(); input.setSelectionRange(input.value.length, input.value.length);
  });

  log.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-confirm]");
    if (t) {
      const box = t.closest(".confirm"), id = box.dataset.action;
      box.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try {
        const r = await api(`/api/actions/${id}/${t.dataset.confirm === "1" ? "confirm" : "reject"}`, { method: "POST" });
        state.pending.delete(id); state.lastTrace = r.trace_id;
        box.querySelector(".row").outerHTML = `<div class="done">${esc(r.text)}</div>`;
        loadSide().then(refreshStars);
      } catch (err) { box.querySelector(".row").outerHTML = `<div class="done">${esc(err.message)}</div>`; }
      return;
    }
    const q = e.target.closest(".note [data-q]"); if (q) { send(q.dataset.q); return; }
    const c = e.target.closest(".ap.collapsed"); if (c) expandPanel(c, true);
  });
  log.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches(".ap.collapsed")) expandPanel(e.target, true); });

  // =================== отчёт ===================
  function collapseOthers(keep) { log.querySelectorAll(".ap").forEach((p) => { if (p !== keep && !p.classList.contains("collapsed")) collapsePanel(p); }); }

  function collapsePanel(el) {
    const r = el._r, m = r.metrics, b = r.benchmark;
    el.querySelectorAll(".plot").forEach((p) => Plotly.purge(p));
    el.className = "ap collapsed"; el.setAttribute("role", "button"); el.tabIndex = 0;
    el.setAttribute("aria-label", `Развернуть отчёт: ${company(r.spec.ticker)}`);
    el.innerHTML = `<div class="ap-mini"><span class="t">${esc(company(r.spec.ticker))} (${r.spec.ticker}), ${esc(strategyText(r.spec))}, ${period(r.spec)}</span>
      <span class="n"><span>капитал <b>${rub(m.final_equity)}</b></span><span>к Buy &amp; Hold <b class="${tone(m.final_equity - b.final_equity)}">${rub(m.final_equity - b.final_equity, true)}</b></span><span>развернуть</span></span></div>`;
  }

  function expandPanel(el, animate = false) {
    if (!el.classList.contains("collapsed") && el.childElementCount) { setActive(el._r.run_id); return; }
    collapseOthers(el);
    const r = el._r;
    el.className = "ap" + (animate ? " reveal" : ""); el.removeAttribute("role"); el.removeAttribute("aria-label"); el.tabIndex = -1;
    el.innerHTML = panelHtml(r);
    wirePanel(el, r);
    setActive(r.run_id);
    plotEquity(el, r);
    if (el._detailOpen) openDetail(el, el._tab || "risk");
  }

  const isFav = (runId) => state.favs.some((x) => x.run_id === runId);
  function refreshStars() {
    log.querySelectorAll(".ap[data-run] [data-star]").forEach((b) => {
      const on = isFav(b.closest(".ap").dataset.run);
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on));
      b.setAttribute("aria-label", on ? "Убрать из избранного" : "Добавить в избранное"); b.title = b.getAttribute("aria-label");
      b.querySelector("use").setAttribute("href", on ? "#i-star-fill" : "#i-star");
    });
  }

  function hero(r) {
    const m = r.metrics, b = r.benchmark, diff = m.final_equity - b.final_equity;
    return `<div class="hero">
      <div class="hk k1"><div class="l">Итоговый капитал</div><div class="v">${rub(m.final_equity)}</div><div class="s">из ${rub(r.spec.initial_cash)} начального</div></div>
      <div class="hk k2"><div class="l">${term("Против Buy &amp; Hold", "vs")}</div><div class="v ${tone(diff)}">${rub(diff, true)}</div><div class="s">держание дало бы ${rub(b.final_equity)}</div></div>
      <div class="hk k3"><div class="l">Доходность стратегии</div><div class="v ${tone(m.total_return_pct)}">${pct(m.total_return_pct, true, 1)}</div><div class="s">у ${term("Buy &amp; Hold", "hold")} ${pct(b.total_return_pct, true, 1)}</div></div></div>`;
  }

  function understoodHtml(r) {
    const u = r.understood || {}, s = r.spec;
    if (!u.entry) return "";
    const line = [u.entry, u.exit].filter(Boolean).join(" ");
    return `<details class="understood"><summary><span class="u-h">Как я понял стратегию</span><span class="u-line">${esc(line)}</span><span class="u-tg">подробнее</span></summary>
      <div class="u-b"><p>${withTerms(esc(u.entry))}</p>${u.exit ? `<p>${withTerms(esc(u.exit))}</p>` : ""}
        <dl><dt>Период</dt><dd>${dmy(s.start_date)} - ${dmy(s.end_date)}, ${r.data.rows} торговых дней</dd>
          <dt>Капитал</dt><dd>${rub(s.initial_cash)}, всем капиталом, только покупка${(r.assumptions || []).some((x) => x.startsWith("капитал не указан")) ? `<span class="muted"> (по умолчанию: напишите «капитал 500 тысяч» или поменяйте в настройках)</span>` : ""}</dd>
          <dt>Исполнение</dt><dd>${esc(u.execution || "решение по закрытию дня, сделка по открытию следующего")}</dd>
          <dt>Издержки</dt><dd>комиссия ${fmt(s.commission * 100, 3)}% с каждой стороны, проскальзывание ${fmt(s.slippage * 100, 3)}%</dd>
          <dt>Котировки</dt><dd>${r.data.is_synthetic ? "демонстрационные, не биржевые" : r.data.source === "cache" ? "MOEX ISS из кеша" : "MOEX ISS"}</dd></dl>
        <p class="fix">Понял не так? Поправьте в чате, например «продавай при RSI выше 65».</p></div></details>`;
  }

  function panelHtml(r) {
    const s = r.spec, v = verdict(r), fav = isFav(r.run_id);
    return `
      <header class="ap-head">
        <div><div class="ap-title">${esc(company(s.ticker))} <span class="tk">${s.ticker}</span></div>
          <div class="ap-sub">${strategyText(s, true)}, ${period(s)}</div></div>
        <div class="ap-actions">
          <button class="act${fav ? " on" : ""}" type="button" data-star aria-pressed="${fav}" aria-label="${fav ? "Убрать из избранного" : "Добавить в избранное"}" title="${fav ? "Убрать из избранного" : "Добавить в избранное"}"><svg><use href="${fav ? "#i-star-fill" : "#i-star"}"/></svg></button>
          <div class="menu"><button class="act" type="button" data-export aria-label="Экспорт" title="Экспорт" aria-haspopup="true" aria-expanded="false"><svg><use href="#i-down"/></svg></button>
            <div class="menu-list" hidden><a href="/api/runs/${r.run_id}/report.html" target="_blank" rel="noopener">Отчёт HTML с графиками</a>
              <a href="/api/runs/${r.run_id}/trades.csv">Сделки, CSV</a>
              <button type="button" data-tech>Технические детали</button></div></div>
        </div>
      </header>
      ${v.warns.length ? `<div class="warns">${v.warns.map(([c, t]) => `<span class="warn ${c}">${t}</span>`).join("")}</div>` : ""}
      <p class="ap-verdict">${v.line}</p>
      ${hero(r)}
      ${understoodHtml(r)}
      <div class="chart-card">
        <div class="chart-h"><b>Динамика капитала <span class="term" tabindex="0" data-tip="${esc(TERMS.equity)}"><svg><use href="#i-info"/></svg></span></b>
          <div class="legend2"><span><i style="background:var(--ch-strategy)"></i>Стратегия</span>
            <button type="button" data-bench aria-pressed="true"><i class="dash"></i>Buy &amp; Hold</button>
            ${periodHtml()}</div></div>
        <div class="plot" data-plot="eq" role="img" aria-label="График капитала стратегии и Buy and Hold"></div>
      </div>
      <button class="more-btn" type="button" aria-expanded="false" data-more>Подробная аналитика<svg><use href="#i-chev"/></svg></button>
      <div class="detail" hidden>
        <div class="tabs" role="tablist">
          ${[["risk", "Риск и метрики"], ["price", "Цена и сделки"], ["trades", `Сделки (${r.trades.length})`], ["findings", "Выводы"], ["check", "Проверка и новости"]]
            .map(([k, l]) => `<button class="tab" type="button" role="tab" data-tab="${k}" aria-selected="false">${l}</button>`).join("")}
        </div>
        <div class="pane" data-pane="risk" role="tabpanel" hidden>${riskHtml(r)}</div>
        <div class="pane" data-pane="price" role="tabpanel" hidden>${priceHtml(r)}</div>
        <div class="pane" data-pane="trades" role="tabpanel" hidden>${tradesHtml(r)}</div>
        <div class="pane" data-pane="findings" role="tabpanel" hidden>${findingsHtml(r)}</div>
        <div class="pane" data-pane="check" role="tabpanel" hidden>${checkHtml(r)}</div>
      </div>
      <div class="ap-foot"><span>Не является индивидуальной инвестиционной рекомендацией.</span></div>`;
  }

  function wirePanel(el, r) {
    el.querySelector("[data-more]").addEventListener("click", () => {
      const open = el.querySelector(".detail").hidden;
      if (open) openDetail(el, el._tab || "risk"); else { el.querySelector(".detail").hidden = true; el._detailOpen = false; el.querySelector("[data-more]").setAttribute("aria-expanded", "false"); }
    });
    el.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => openDetail(el, b.dataset.tab)));
    el.querySelector(".tabs").addEventListener("keydown", (e) => {
      if (!["ArrowRight", "ArrowLeft"].includes(e.key)) return;
      const tabs = [...el.querySelectorAll("[data-tab]")], i = tabs.indexOf(document.activeElement);
      const n = tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length]; n.focus(); n.click();
    });
    el.querySelector("[data-bench]").addEventListener("click", (e) => {
      const b = e.currentTarget, on = b.getAttribute("aria-pressed") !== "true"; b.setAttribute("aria-pressed", String(on));
      Plotly.restyle(el.querySelector('[data-plot="eq"]'), { visible: on }, [1]);
    });
    el.querySelectorAll(".reset").forEach((b) => b.addEventListener("click", () => zoom(el, null)));
    el.addEventListener("click", (e) => {
      const b = e.target.closest("[data-period]"); if (!b || !el.contains(b)) return;
      const k = b.dataset.period;
      if (k === "all") { zoom(el, null); return; }
      const end = r.equity[r.equity.length - 1].date, d = new Date(end); d.setMonth(d.getMonth() - +k);
      const start = d.toISOString().slice(0, 10);
      zoom(el, [start < r.equity[0].date ? r.equity[0].date : start, end], k);
    });
    const menu = el.querySelector(".ap-actions .menu"); bindMenu(menu.querySelector("[data-export]"), menu.querySelector(".menu-list"));
    menu.querySelector("[data-tech]").addEventListener("click", () => { menu.querySelector(".menu-list").hidden = true; openDetails(r); });
    el.querySelector("[data-star]").addEventListener("click", async () => {
      try {
        const f = state.favs.find((x) => x.run_id === r.run_id);
        const ok = f ? await humanAction(() => api(`/api/research/${f.id}/delete?session_id=${sid()}`, { method: "POST" }))
                     : await humanAction(() => api(`/api/runs/${r.run_id}/save`, { method: "POST", body: JSON.stringify({ session_id: sid() }) }));
        if (ok) { await loadSide(); refreshStars(); }
      } catch (err) { toast("Не удалось: " + err.message); }
    });
    el.querySelector('[data-pane="check"] [data-hood]')?.addEventListener("click", () => openDetails(r));
    wireTrades(el, r);
    wirePrice(el, r);
  }

  function openDetail(el, tab) {
    const det = el.querySelector(".detail");
    det.hidden = false; el._detailOpen = true; el._tab = tab;
    el.querySelector("[data-more]").setAttribute("aria-expanded", "true");
    el.querySelectorAll("[data-tab]").forEach((b) => { b.setAttribute("aria-selected", String(b.dataset.tab === tab)); b.tabIndex = b.dataset.tab === tab ? 0 : -1; });
    el.querySelectorAll("[data-pane]").forEach((p) => (p.hidden = p.dataset.pane !== tab));
    if (tab === "price") requestAnimationFrame(() => plotPrice(el, el._r, el._pv || "price"));
    if (tab === "risk") requestAnimationFrame(() => plotDD(el, el._r));
    if (tab === "check") loadTrace(el);
  }

  // ---------- уровень 2: риск и метрики ----------
  function riskHtml(r) {
    const m = r.metrics, b = r.benchmark;
    const t = (l, v, s = "", c = "") => `<div class="mt"><div class="l">${l}</div><div class="v ${c}">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`;
    return `<div class="metrics">
      ${t(term("Макс. просадка", "dd"), pct(-Math.abs(m.max_drawdown_pct), false, 1), `у Buy &amp; Hold ${pct(-Math.abs(b.max_drawdown_pct), false, 1)}`)}
      ${t(term("Сделок", "trades"), String(r.trades.length), m.open_position_at_end ? "последняя ещё открыта" : "")}
      ${t(term("В плюс", "win"), m.trades_count ? fmt(m.win_rate_pct, 0) + "%" : "н/д", `из ${m.trades_count} закрытых`)}
      ${t(term("Buy &amp; Hold", "hold"), pct(b.total_return_pct, true, 1), rub(b.final_equity), tone(b.total_return_pct))}
      ${t(term("CAGR", "cagr"), pct(m.cagr_pct, true, 1), "в среднем за год", tone(m.cagr_pct))}
      ${t(term("Коэффициент Шарпа", "sharpe"), fmt(m.sharpe), `у Buy &amp; Hold ${fmt(b.sharpe)}`)}
      ${t(term("Время в позиции", "exposure"), fmt(m.exposure_pct, 0) + "%")}
      ${t("Комиссии", rub(m.commissions_paid), `волатильность ${pct(m.volatility_pct, false, 1)}`)}
    </div>
    <div class="chart-card dd-card"><div class="chart-h"><b>${term("Просадка", "dd")} по дням</b>
      <div class="legend2"><span><i style="background:var(--neg)"></i>Стратегия</span><span><i class="dash"></i>Buy &amp; Hold</span></div></div>
      <div class="plot" data-plot="dd" role="img" aria-label="График просадки капитала стратегии и Buy and Hold"></div></div>`;
  }

  // кнопки периода: одна шкала времени на все графики отчёта
  const PERIODS = [["1", "1 мес"], ["6", "6 мес"], ["12", "1 год"], ["all", "Всё"]];
  const periodHtml = () => `<div class="pseg" role="group" aria-label="Период на графиках">${PERIODS.map(([k, l]) =>
    `<button type="button" data-period="${k}" aria-pressed="${k === "all"}">${l}</button>`).join("")}</div>`;

  // ---------- уровень 2: цена и сделки ----------
  const volumeOk = (r) => {
    const v = r.equity.map((e) => e.volume);
    return v.some((x) => x != null) && v.every((x) => x == null || (isFinite(x) && x >= 0)) && v.some((x) => x > 0);
  };
  function priceHtml(r) {
    const ind = r.diagnostics.indicators || {};
    const osc = Object.keys(ind).find((k) => k.startsWith("osc:"));
    const overlays = Object.keys(ind).some((k) => !k.startsWith("osc:"));
    const hasOhlc = r.equity.length && r.equity[0].open != null;
    const views = [["price", osc ? `Цена и ${osc.slice(4).replace(/^линия /, "")}` : "Цена и сигналы"], ["volume", "Объём"]];
    return `<div class="chart-h"><div class="subtabs" role="group" aria-label="Что показать">${views.map(([k, l], i) =>
        `<button type="button" data-pv="${k}" aria-pressed="${i === 0}">${esc(l)}</button>`).join("")}</div>
      <div class="toggles" data-price-tg>${hasOhlc ? `<button class="chip-t" type="button" data-tg="candles" aria-pressed="false">Свечи</button>` : ""}
        ${overlays ? `<button class="chip-t" type="button" data-tg="overlays" aria-pressed="false">Линии из правил</button>` : ""}
        ${periodHtml()}</div></div>
      <div class="plot" data-plot="px" role="img" aria-label="График цены с точками покупок и продаж"></div>
      <div class="empty" data-px-empty hidden></div>
      <div class="legend2" data-px-legend><span><i class="dot" style="background:var(--ch-strategy)"></i>покупка</span><span><i class="dot" style="background:var(--neg)"></i>продажа</span>${(r.events || []).some((e) => e.news.length) ? `<span><i class="dot ring"></i>новости рядом</span>` : ""}
        <span class="muted">Наведите на треугольник, чтобы увидеть сделку; клик выделит её в таблице.</span></div>`;
  }
  function wirePrice(el, r) {
    el.querySelectorAll("[data-pv]").forEach((b) => b.addEventListener("click", () => {
      el.querySelectorAll("[data-pv]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      el._pv = b.dataset.pv; plotPrice(el, r, el._pv);
      // свечи и линии относятся только к графику цены: на объёме их не показываем
      el.querySelectorAll("[data-price-tg] [data-tg]").forEach((t) => (t.hidden = el._pv === "volume"));
    }));
    el.querySelectorAll("[data-tg]").forEach((b) => b.addEventListener("click", () => {
      b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true"));
      el["_" + b.dataset.tg] = b.getAttribute("aria-pressed") === "true"; plotPrice(el, r, el._pv || "price");
    }));
  }

  // ---------- графики ----------
  const fullRange = (el, r) => (el._full = el._full || [r.equity[0].date, r.equity[r.equity.length - 1].date]);
  function resizePlots() { document.querySelectorAll(".plot").forEach((p) => { if (p.data && p.offsetParent) Plotly.Plots.resize(p); }); }

  function plotEquity(el, r) {
    const p = el.querySelector('[data-plot="eq"]'); if (!p) return;
    const x = r.equity.map((e) => e.date);
    const s = r.equity.map((e) => e.equity), b = r.equity.map((e) => e.benchmark), cash = r.spec.initial_cash;
    const lo = Math.min(...s, ...b, cash), hi = Math.max(...s, ...b, cash), pad = (hi - lo) * 0.08 || cash * 0.02;
    const benchOn = el.querySelector("[data-bench]")?.getAttribute("aria-pressed") !== "false";
    Plotly.react(p, [
      { x, y: s, name: "Стратегия", line: { color: C.mint, width: 2 }, hovertemplate: "%{y:,.0f} ₽" },
      { x, y: b, name: "Buy & Hold", line: { color: C.bench, width: 1.3, dash: "dash" }, hovertemplate: "%{y:,.0f} ₽", visible: benchOn },
    ], { ...BASE, height: p.clientHeight || 260,
      xaxis: { ...XAX, range: el._range || fullRange(el, r), autorange: false },
      yaxis: { ...AX, tickformat: ",.0f", ticksuffix: " ₽", nticks: 5, ...(el._range ? fitY(el, r, "eq") : { range: [lo - pad, hi + pad] }) },
      shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: cash, y1: cash, line: { color: C.start, width: 1, dash: "dot" } }],
      annotations: [{ xref: "paper", x: 1, y: cash, yanchor: "top", xanchor: "right", text: `старт ${rub(cash)}`, showarrow: false, yshift: -2,
        font: { size: 10, color: C.muted } }] }, CFG);
    syncRange(el, p);
  }

  // просадка: насколько капитал ниже своего прошлого максимума, в процентах
  function plotDD(el, r) {
    const p = el.querySelector('[data-plot="dd"]'); if (!p || !p.offsetParent) return;
    const x = r.equity.map((e) => e.date), d = r.equity.map((e) => e.drawdown ?? null);
    const bd = r.equity.map((e) => e.benchmark_drawdown ?? null);
    Plotly.react(p, [
      { x, y: d, name: "Стратегия", fill: "tozeroy", fillcolor: C.negSoft, line: { color: C.neg, width: 1.4 }, hovertemplate: "%{y:.1f}%" },
      { x, y: bd, name: "Buy & Hold", line: { color: C.bench, width: 1.1, dash: "dash" }, hovertemplate: "%{y:.1f}%", visible: bd.some((v) => v != null) },
    ], { ...BASE, height: p.clientHeight || 200, xaxis: { ...XAX, range: el._range || fullRange(el, r), autorange: false },
      yaxis: { ...AX, ticksuffix: "%", nticks: 4, ...fitY(el, r, "dd") } }, CFG);
    syncRange(el, p);
  }

  // масштаб по вертикали под видимый отрезок: иначе приближенная сделка выглядит плоской линией
  function visibleRows(el, r) {
    const [a, b] = el._range || fullRange(el, r);
    const rows = r.equity.filter((e) => e.date >= a.slice(0, 10) && e.date <= b.slice(0, 10));
    return rows.length > 1 ? rows : r.equity;
  }
  function fitY(el, r, kind) {
    const rows = visibleRows(el, r), pad = (lo, hi, k = 0.08) => { const d = (hi - lo) || Math.abs(hi) * 0.02 || 1; return [lo - d * k, hi + d * k]; };
    if (kind === "px") {
      const lo = Math.min(...rows.map((e) => e.low ?? e.close)), hi = Math.max(...rows.map((e) => e.high ?? e.close));
      const [a, b] = pad(lo, hi, 0.06); return { range: [Math.min(a, lo * 0.975), b + Math.max((hi - lo) * 0.08, hi * 0.05)], autorange: false };
    }
    if (kind === "vol") return { range: [0, Math.max(...rows.map((e) => e.volume || 0)) * 1.1 || 1], autorange: false };
    if (kind === "eq") {
      const v = rows.flatMap((e) => [e.equity, e.benchmark]).concat(el._range ? [] : [r.spec.initial_cash]);
      return { range: pad(Math.min(...v), Math.max(...v)), autorange: false };
    }
    if (kind === "dd") {
      const lo = Math.min(...rows.flatMap((e) => [e.drawdown ?? 0, e.benchmark_drawdown ?? 0]));
      return { range: [Math.min(lo * 1.08, -1), 0.5], autorange: false };
    }
    return {};
  }
  const plotKind = (o, el) => o.dataset.plot === "px" ? ((el._pv || "price") === "volume" ? "vol" : "px") : o.dataset.plot;

  function tradeAt(r) {           // номер сделки по дате входа и выхода, для подсказок и выбора
    const ent = {}, ex = {};
    r.trades.forEach((t, i) => { ent[t.entry_date] = i; if (t.exit_date) ex[t.exit_date] = i; });
    return { ent, ex };
  }

  function plotPrice(el, r, view) {
    const p = el.querySelector('[data-plot="px"]'), empty = el.querySelector("[data-px-empty]"); if (!p) return;
    const eq = r.equity, x = eq.map((e) => e.date), byDate = Object.fromEntries(eq.map((e) => [e.date, e]));
    const ind = r.diagnostics.indicators || {};
    const oscKey = Object.keys(ind).find((k) => k.startsWith("osc:"));
    const ent = r.trades.map((t, i) => ({ ...t, n: i + 1 })).filter((t) => byDate[t.entry_date]);
    const ex = r.trades.map((t, i) => ({ ...t, n: i + 1 })).filter((t) => t.exit_date && byDate[t.exit_date]);
    const entText = (t) => `Сделка ${t.n}: покупка ${dmy(t.entry_date)}<br>${t.size.toLocaleString("ru-RU")} бумаг по ${fmt(t.entry_price)} ₽`;
    const exText = (t) => `Сделка ${t.n}: продажа ${dmy(t.exit_date)} по ${fmt(t.exit_price)} ₽${reasonText(t) ? `, ${reasonText(t)}` : ""}<br>${rub(t.pnl, true)} (${pct(t.return_pct)}), ${t.holding_days} дн.`;
    const markers = (yIn, yOut) => [
      { x: ent.map((t) => t.entry_date), y: ent.map(yIn), customdata: ent.map((t) => t.n), mode: "markers", name: "Покупка",
        marker: { symbol: "triangle-up", size: 11, color: C.mint, line: { color: C.bg, width: 1 } }, text: ent.map(entText), hovertemplate: "%{text}<extra></extra>" },
      { x: ex.map((t) => t.exit_date), y: ex.map(yOut), customdata: ex.map((t) => t.n), mode: "markers", name: "Продажа",
        marker: { symbol: "triangle-down", size: 11, color: C.neg, line: { color: C.bg, width: 1 } }, text: ex.map(exText), hovertemplate: "%{text}<extra></extra>" }];
    let traces, layout;
    p.hidden = false; empty.hidden = true; el.querySelector("[data-px-legend]").hidden = view === "volume";
    if (view === "volume") {
      if (!volumeOk(r)) {                    // честное пустое состояние вместо графика с выдуманной осью
        Plotly.purge(p); p.hidden = true; empty.hidden = false;
        empty.textContent = "Объёма в этом анализе нет: он посчитан до того, как объём начали сохранять. Повторите запрос, и объём появится.";
        return;
      }
      traces = [{ x, y: eq.map((e) => e.volume ?? null), type: "bar", name: "Объём", marker: { color: C.vol }, hovertemplate: "%{y:,.0f} бумаг<extra>Объём</extra>" }];
      layout = { ...BASE, xaxis: XAX, yaxis: { ...AX, side: "right", rangemode: "nonnegative", tickformat: "~s", nticks: 4 }, margin: { ...BASE.margin, l: 10, r: 48 } };
    } else if (view === "osc" && oscKey) {
      const y = ind[oscKey], name = oscKey.slice(4), idx = Object.fromEntries(x.map((d, i) => [d, i]));
      const levels = /RSI/.test(name) ? [30, 70] : /стохастик|%K|%D/i.test(name) ? [20, 80] : [0];
      traces = [{ x, y, name, line: { color: C.osc, width: 1.4 }, hovertemplate: "%{y:.1f}" }, ...markers((t) => y[idx[t.entry_date]], (t) => y[idx[t.exit_date]])];
      layout = { ...BASE, xaxis: XAX, yaxis: { ...AX, side: "right", nticks: 5 }, margin: { ...BASE.margin, l: 10, r: 44 },
        shapes: levels.map((v) => ({ type: "line", xref: "paper", x0: 0, x1: 1, y0: v, y1: v, line: { color: C.level, width: 1, dash: "dash" } })) };
    } else {
      const candles = el._candles && eq[0].open != null;
      traces = [candles
        ? { x, open: eq.map((e) => e.open), high: eq.map((e) => e.high), low: eq.map((e) => e.low), close: eq.map((e) => e.close), type: "candlestick", name: "Цена",
            increasing: { line: { color: C.mint, width: 1 }, fillcolor: C.mint }, decreasing: { line: { color: C.neg, width: 1 }, fillcolor: C.neg }, whiskerwidth: 0 }
        : { x, y: eq.map((e) => e.close), name: "Цена", line: { color: C.price, width: 1.3 }, hovertemplate: "%{y:.2f} ₽<extra>Цена</extra>" }];
      if (el._overlays) Object.entries(ind).filter(([k]) => !k.startsWith("osc:")).forEach(([k, v], i) =>
        traces.push({ x, y: v, name: k, line: { color: C.ov[i % 3], width: 1 }, hovertemplate: "%{y:.2f}" }));
      const lo = (d) => (byDate[d].low ?? byDate[d].close), hi = (d) => (byDate[d].high ?? byDate[d].close);
      traces.push(...markers((t) => lo(t.entry_date) * 0.985, (t) => hi(t.exit_date) * 1.015));
      // дни резких движений с найденными новостями: кружок над ценой, в подсказке заголовки
      const evs = (r.events || []).filter((e) => byDate[e.date] && e.news.some((n) => !n.flagged_injection));
      if (evs.length) traces.push({ x: evs.map((e) => e.date), y: evs.map((e) => hi(e.date) * 1.04), mode: "markers", name: "Новости",
        marker: { symbol: "circle", size: 8, color: "rgba(0,0,0,0)", line: { color: C.osc, width: 1.6 } },
        text: evs.map((e) => `${dmy(e.date)}: цена ${pct(e.change_pct)}<br>` + e.news.filter((n) => !n.flagged_injection).slice(0, 3)
          .map((n) => esc(n.title.length > 90 ? n.title.slice(0, 88) + "…" : n.title)).join("<br>")),
        hovertemplate: "%{text}<extra>Новости рядом</extra>" });
      layout = { ...BASE, hovermode: "closest", xaxis: { ...XAX, rangeslider: { visible: false } },
        yaxis: { ...AX, side: "right", nticks: 5, ticksuffix: " ₽", ...fitY(el, r, "px") }, margin: { ...BASE.margin, l: 10, r: 60 } };
      // индикатор стратегии под ценой на общей шкале времени, с теми же точками сделок
      if (oscKey) {
        const y = ind[oscKey], name = oscKey.slice(4), idx = Object.fromEntries(x.map((d, i) => [d, i]));
        const levels = /RSI/.test(name) ? [30, 70] : /стохастик|%K|%D/i.test(name) ? [20, 80] : [0];
        traces.push({ x, y, name, yaxis: "y2", line: { color: C.osc, width: 1.3 }, hovertemplate: `%{y:.1f}<extra>${esc(name)}</extra>` },
          ...markers((t) => y[idx[t.entry_date]], (t) => y[idx[t.exit_date]]).map((m) => ({ ...m, yaxis: "y2", marker: { ...m.marker, size: 8 } })));
        layout.yaxis.domain = [0.34, 1];
        const bounded = levels.length === 2;          // RSI и стохастик живут в 0..100: ось фиксирована, подписаны уровни
        layout.yaxis2 = { ...AX, side: "right", domain: [0, 0.25], anchor: "x",
          ...(bounded ? { range: [0, 100], tickvals: levels, autorange: false } : { nticks: 3 }) };
        layout.shapes = levels.map((v) => ({ type: "line", xref: "paper", yref: "y2", x0: 0, x1: 1, y0: v, y1: v, line: { color: C.level, width: 1, dash: "dash" } }));
        layout.annotations = [{ xref: "paper", yref: "paper", x: 0, y: 0.27, xanchor: "left", yanchor: "bottom", showarrow: false,
          text: esc(name.replace(/^линия /, "")), font: { size: 10, color: C.muted } }];
      }
      p.style.height = oscKey ? "420px" : "";
    }
    if (view === "volume") { p.style.height = ""; layout.yaxis = { ...layout.yaxis, ...fitY(el, r, "vol") }; }
    layout.height = p.clientHeight || 260;
    layout.xaxis = { ...layout.xaxis, range: el._range || fullRange(el, r), autorange: false };
    if (el._hl) layout.shapes = (layout.shapes || []).concat([el._hl]);
    Plotly.react(p, traces, layout, CFG);
    syncRange(el, p);
    if (!p._clickBound) { p._clickBound = true;
      p.on("plotly_click", (ev) => { const n = ev.points?.[0]?.customdata; if (n) selectTrade(el, r, n, false); }); }
  }

  // единая временная шкала: масштаб одного графика переносится на остальные, вертикаль подстраивается
  function syncRange(el, p) {
    if (p._synced) return; p._synced = true;
    p.on("plotly_relayout", (ev) => {
      if (el._syncing) return;
      const a = ev["xaxis.range[0]"], b = ev["xaxis.range[1]"], auto = ev["xaxis.autorange"];
      if (a === undefined && !auto) return;
      if (auto) { zoom(el, null); return; }
      zoom(el, [String(a).slice(0, 10), String(b).slice(0, 10)], null, true);
    });
  }
  function zoom(el, range, periodKey = null, keepHl = false) {
    const r = el._r;
    el._range = range ? [range[0], range[1]] : null; el._zoomed = !!range;
    el._syncing = true; setTimeout(() => (el._syncing = false), 0);
    if (!keepHl) el._hl = range && range[2] ? { type: "rect", xref: "x", yref: "paper", x0: range[2], x1: range[3], y0: 0, y1: 1, fillcolor: C.hl, line: { width: 0 } } : null;
    if (!range) el.querySelectorAll(".tbl tr.sel").forEach((t) => t.classList.remove("sel"));
    el.querySelectorAll(".plot").forEach((o) => { if (!o.data) return;
      const kind = plotKind(o, el), base = (o.layout.shapes || []).filter((s) => s.xref !== "x");
      const fit = fitY(el, r, kind);
      Plotly.relayout(o, { "xaxis.range": el._range || fullRange(el, r), "xaxis.autorange": false,
        ...(fit.range ? { "yaxis.range": fit.range, "yaxis.autorange": false } : {}),
        shapes: o.dataset.plot === "px" && el._hl ? base.concat([el._hl]) : base }); });
    el.querySelectorAll(".reset").forEach((x) => (x.hidden = !range));
    const pk = range ? periodKey : "all";
    el.querySelectorAll("[data-period]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.period === pk)));
  }

  // выбранная сделка: строка в таблице и участок на графиках
  function selectTrade(el, r, n, goToChart) {
    const t = r.trades[n - 1]; if (!t) return;
    el._selN = n;
    el.querySelectorAll(".tbl tbody tr").forEach((tr) => tr.classList.toggle("sel", +tr.dataset.n === n));
    const end = t.exit_date || r.equity[r.equity.length - 1].date, span = 20 * 864e5;
    const a = new Date(new Date(t.entry_date) - span).toISOString().slice(0, 10), b = new Date(+new Date(end) + span).toISOString().slice(0, 10);
    if (goToChart) {
      el._pv = "price"; el.querySelectorAll("[data-pv]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.pv === "price")));
      openDetail(el, "price"); requestAnimationFrame(() => { zoom(el, [a, b, t.entry_date, end]); el.querySelector('[data-pane="price"]').scrollIntoView({ block: "nearest", behavior: "smooth" }); }); }
    else zoom(el, [a, b, t.entry_date, end]);
  }

  // ---------- сделки ----------
  const EXIT_REASON = { signal: "сигнал", stop_loss: "стоп-лосс", take_profit: "тейк-профит", trailing: "трейлинг-стоп",
    max_hold: "срок удержания", atr_stop: "стоп по ATR", end: "не закрыта" };
  const reasonText = (t) => EXIT_REASON[t.exit_reason] || "";
  function reasonSummary(trades) {
    const closed = trades.filter((t) => !t.is_open && t.exit_reason), cnt = {};
    closed.forEach((t) => { cnt[t.exit_reason] = (cnt[t.exit_reason] || 0) + 1; });
    const keys = Object.keys(cnt).sort((a, b) => cnt[b] - cnt[a]);
    if (!keys.length || (keys.length === 1 && keys[0] === "signal")) return "";   // одна причина «сигнал» ничего не добавляет
    return " Причины выхода: " + keys.map((k) => `${EXIT_REASON[k]} ${cnt[k]}`).join(", ") + ".";
  }
  const COLS = [["n", "№", true], ["entry_date", "Вход"], ["exit_date", "Выход"], ["entry_price", "Цена входа", true], ["exit_price", "Цена выхода", true], ["exit_reason", "Причина"],
    ["size", "Бумаг", true], ["pnl", "Результат, ₽", true], ["return_pct", "Доходность", true], ["holding_days", "Дней", true]];
  function tradesHtml(r) {
    if (!r.trades.length) return `<div class="empty">Сделок нет: условие входа ни разу не сработало. Рынок не дал повода.</div>`;
    const closed = r.trades.filter((t) => !t.is_open), m = r.metrics;
    return `<div class="trades-sum">Все сделки в длинную сторону: покупка, затем продажа. Закрытых ${closed.length}, в плюс ${fmt(m.win_rate_pct, 0)}%${m.open_position_at_end ? ", последняя открыта и оценена по цене закрытия" : ""}.${reasonSummary(r.trades)} Нажмите на строку, чтобы увидеть сделку на графике.</div>
      <div class="tbl-scroll"><table class="tbl"><thead><tr>${COLS.map(([k, l, num]) => `<th class="${num ? "r" : ""}" data-k="${k}" aria-sort="none"><button type="button">${l}</button></th>`).join("")}</tr></thead><tbody></tbody></table></div>`;
  }
  function wireTrades(el, r) {
    const tb = el.querySelector(".tbl tbody"); if (!tb) return;
    let rows = r.trades.map((t, i) => ({ ...t, n: i + 1 })), sortK = "n", dir = 1;
    const draw = () => {
      tb.innerHTML = rows.map((t) => `<tr tabindex="0" data-n="${t.n}">
        <td class="r">${t.n}</td><td>${dmy(t.entry_date)}</td><td>${t.exit_date ? dmy(t.exit_date) : '<span class="muted">открыта</span>'}</td>
        <td class="r">${fmt(t.entry_price)}</td><td class="r">${t.exit_price != null ? fmt(t.exit_price) : ""}</td><td class="${t.exit_reason === "signal" || !t.exit_reason ? "muted" : ""}">${t.is_open ? "" : reasonText(t)}</td><td class="r">${t.size.toLocaleString("ru-RU")}</td>
        <td class="r ${tone(t.pnl)}">${rub(t.pnl, true)}</td><td class="r ${tone(t.return_pct)}">${pct(t.return_pct)}</td><td class="r">${t.holding_days}</td></tr>`).join("");
      el.querySelectorAll(".tbl th[data-k]").forEach((th) => th.setAttribute("aria-sort", th.dataset.k === sortK ? (dir > 0 ? "ascending" : "descending") : "none"));
    };
    el.querySelectorAll(".tbl th[data-k] button").forEach((b) => b.addEventListener("click", () => {
      const k = b.parentElement.dataset.k; dir = k === sortK ? -dir : 1; sortK = k;
      rows.sort((a, c) => { const x = a[k] ?? "", y = c[k] ?? ""; return (x > y ? 1 : x < y ? -1 : 0) * dir; }); draw();
    }));
    tb.addEventListener("click", (e) => { const tr = e.target.closest("tr"); if (tr) selectTrade(el, r, +tr.dataset.n, true); });
    tb.addEventListener("keydown", (e) => { if (e.key === "Enter") { const tr = e.target.closest("tr"); if (tr) selectTrade(el, r, +tr.dataset.n, true); } });
    draw();
  }

  // ---------- выводы: только посчитанное, без торговых советов ----------
  function conclusions(r) {
    const m = r.metrics, b = r.benchmark, diff = m.final_equity - b.final_equity, out = [];
    if (!r.trades.length) out.push("Условие входа ни разу не выполнилось, капитал не изменился.");
    else {
      out.push(`Стратегия: ${pct(m.total_return_pct)} за период, это на ${rub(Math.abs(diff))} ${diff >= 0 ? "больше" : "меньше"}, чем Buy &amp; Hold (${pct(b.total_return_pct)}).`);
      out.push(`Максимальная просадка ${fmt(Math.abs(m.max_drawdown_pct))}% против ${fmt(Math.abs(b.max_drawdown_pct))}% у Buy &amp; Hold.`);
      const closed = r.trades.filter((t) => !t.is_open);
      if (closed.length) {
        const avg = closed.reduce((s, t) => s + t.return_pct, 0) / closed.length;
        out.push(`Закрытых сделок ${closed.length}, в плюс ${fmt(m.win_rate_pct, 0)}%, средняя сделка ${pct(avg)}.`);
      }
      out.push(`В позиции ${fmt(m.exposure_pct, 0)}% времени, комиссии ${rub(m.commissions_paid)}.`);
      if (diff > 0) out.push("Выигрыш получен на одном историческом отрезке: это не прогноз, проверьте другие периоды.");
    }
    return out;
  }
  function nextChecks(r) {
    const out = (r.diagnostics.findings || []).map((x) => withTerms(esc(x))).slice(0, 3);
    const closed = r.trades.filter((t) => !t.is_open).length;
    if (closed > 0 && closed < 5) out.push("Сделок меньше пяти: по ним сложно судить о стратегии, проверьте более длинный период.");
    if (r.data.is_synthetic) out.push("Котировки демонстрационные: для выводов запустите сервис без флага --demo.");
    if (!out.length) out.push("Явных слабых мест диагностика не нашла. Проверьте ту же стратегию на другом периоде и бумаге.");
    return out;
  }
  function findingsHtml(r) {
    const sug = (document.querySelector(`.ap[data-run="${r.run_id}"]`)?._sugg) || [];
    return `<div class="cards2"><div class="note"><h4>Выводы</h4><ul>${conclusions(r).map((x) => `<li>${x}</li>`).join("")}</ul></div>
      <div class="note tint"><h4>Что проверить дальше</h4><ul>${nextChecks(r).map((x) => `<li>${x}</li>`).join("")}</ul>
      ${sug.length ? `<div class="chips">${sug.slice(0, 3).map((q) => `<button class="chip" type="button" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>` : ""}</div></div>`;
  }

  // ---------- проверка и новости ----------
  function checkHtml(r) {
    const v = r.verification;
    return `<div class="verif">${v.status === "OK" ? `<svg><use href="#i-check"/></svg><span>Два независимых расчёта совпали по всем ${v.checks.length} проверкам (${term("сверка", "verify")}).</span>`
        : `<span class="neg">Два независимых расчёта не совпали: цифрам доверять нельзя.</span>`}</div>
      <div class="news-list sec"><h5>Новости рядом с резкими движениями</h5>${newsHtml(r)}</div>
`;
  }
  async function loadTrace(el) {
    const box = el.querySelector("[data-trace]"); if (!box || box._done) return; box._done = true;
    if (!state.lastTrace) { box.textContent = "в этой вкладке браузера запросов ещё не было"; return; }
    try {
      const t = await api(`/api/traces/${state.lastTrace}`);
      box.innerHTML = `${t.events.length} шагов, ${t.tokens} токенов, ${fmt(t.cost_rub, 2)} ₽, модель и инструменты ${fmt(t.latency_ms / 1000, 1)} с.`;
    } catch (_) { box.textContent = "трасса недоступна"; }
  }

  // =================== выдвижные панели ===================
  function openDrawer(id) { $("scrim").hidden = false; $(id).hidden = false; $(id).querySelector(".x").focus(); }
  function closeDrawers() { ["details", "settings", "how"].forEach((id) => ($(id).hidden = true)); $("scrim").hidden = true; }
  $("scrim").addEventListener("click", closeDrawers);
  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeDrawers));
  $("howBtn").addEventListener("click", () => { closeMobileSide(); openDrawer("how"); });
  const typing = (t) => t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.isContentEditable);
  const drawerOpen = () => !$("details").hidden || !$("settings").hidden || !$("how").hidden;
  const openReport = () => [...log.querySelectorAll(".ap:not(.collapsed)")].pop();
  document.addEventListener("keydown", (e) => {
    if (!$("modal").hidden) return;
    // ⌘K / Ctrl+K: новый анализ откуда угодно, в том числе из поля ввода
    if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k") { e.preventDefault(); newAnalysis(); return; }
    if (e.key === "Escape") {
      const open = document.querySelectorAll(".menu-list:not([hidden])");
      if (open.length) { open.forEach((m) => (m.hidden = true)); return; }
      if (drawerOpen()) { closeDrawers(); return; }
      if (root.classList.contains("side-open")) { toggleSideMobile(false); return; }
      if (typing(e.target) && e.target.value) return;            // в поле с текстом Esc ничего не стирает
      const ap = openReport();
      if (ap) { collapsePanel(ap); input.focus(); }
      return;
    }
    if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey || drawerOpen()) return;
    if (e.key === "/") { e.preventDefault(); input.focus(); return; }
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      const ap = openReport(); if (!ap || !ap._r || !ap._r.trades.length) return;
      if (e.target.closest && e.target.closest(".tabs, .subtabs, .seg3")) return;   // у вкладок свои стрелки
      e.preventDefault();
      const max = ap._r.trades.length, cur = ap._selN || 0;
      const n = e.key === "ArrowRight" ? Math.min(max, cur + 1) : Math.max(1, (cur || 2) - 1);
      selectTrade(ap, ap._r, n, true);
    }
  });
  // на Windows и Linux вместо ⌘ показываем Ctrl
  if (!/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) document.querySelectorAll("kbd.mod").forEach((k) => (k.textContent = "Ctrl"));
  function bindMenu(btn, list) {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      document.querySelectorAll(".menu-list").forEach((m) => { if (m !== list) m.hidden = true; });
      list.hidden = !list.hidden; btn.setAttribute("aria-expanded", String(!list.hidden));
      if (!list.hidden) list.querySelector("a, button")?.focus();
    });
  }
  document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) document.querySelectorAll(".menu-list").forEach((m) => (m.hidden = true)); });

  function openDetails(r) {
    $("detailsTitle").textContent = `${company(r.spec.ticker)}, под капотом`;
    $("detailsBody").innerHTML = `<div class="sec">${hoodHtml(r)}</div>`;
    openDrawer("details");
    const box = $("traceBox");
    if (box) {
      if (!state.lastTrace) box.textContent = "в этой вкладке браузера запросов ещё не было";
      else api(`/api/traces/${state.lastTrace}`).then((t) => {
        box.innerHTML = `<p>${t.events.length} шагов, ${t.tokens} токенов, ${fmt(t.cost_rub, 4)} ₽, модель и инструменты ${t.latency_ms} мс.</p>
          <table class="table">${t.events.map((e) => `<tr class="${e.kind === "error" ? "bad" : ""}"><td class="num">${e.seq}</td><td>${e.kind}</td><td>${esc(e.name)}</td><td class="num">${e.latency_ms} мс</td></tr>`).join("")}</table>`;
      }).catch(() => (box.textContent = "трасса недоступна"));
    }
  }
  function newsHtml(r) {
    const st = r.news_status || "новости не искались";
    let html = `<p class="muted">${esc(st.charAt(0).toUpperCase() + st.slice(1))}. Новость рядом по дате не означает, что цена изменилась из-за неё.</p>`;
    if (!r.events.length) return html;
    return html + `<div class="scroll"><table class="table"><tr><th>Дата</th><th class="num">Цена</th><th>Новости рядом</th></tr>${
      r.events.map((e) => `<tr><td>${e.date}</td><td class="num ${tone(e.change_pct)}">${pct(e.change_pct)}</td><td>${
        e.news.length ? e.news.map((n) => n.flagged_injection ? `<div><span class="tag">скрыто: попытка управлять агентом</span></div>`
          : `<div>${/^https?:\/\//.test(n.url || "") ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>` : esc(n.title)}
             <span class="muted small">${esc(n.source)}</span></div>`).join("") : `<span class="muted">новостей не нашлось</span>`}</td></tr>`).join("")}</table></div>`;
  }
  function hoodHtml(r) {
    const m = r.metrics, s = r.spec, d = r.diagnostics, v = r.verification;
    const row = (a, b2) => `<tr><td class="muted">${a}</td><td>${b2}</td></tr>`;
    return `<h5>Стратегия и условия</h5><table class="table">
        ${row("Правило", strategyText(s, true))}${row("Бумага", `${esc(company(s.ticker))} (${s.ticker}), режим ${s.board}`)}
        ${row("Период", `с ${s.start_date} по ${s.end_date}, ${r.data.rows} торговых дней`)}${row("Капитал", rub(s.initial_cash))}
        ${row("Издержки", `комиссия ${fmt(s.commission * 100, 3)}% с каждой стороны, проскальзывание ${fmt(s.slippage * 100, 3)}%`)}
        ${row("Исполнение", "решение по закрытию дня, сделка по открытию следующего, только покупка")}
        ${row("Дивиденды", "не учитываются ни в стратегии, ни в держании")}
        ${row("Данные", `${r.data.source}${r.data.is_synthetic ? ", " + term("синтетика", "synth") : ""}`)}</table>
      ${s.rules ? `<h5>Строгая спецификация</h5><pre class="spec">${esc(JSON.stringify(s.rules, null, 2))}</pre>` : ""}
      ${(r.assumptions || []).length ? `<h5>Допущения</h5><ul>${r.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` : ""}
      ${(r.data.warnings || []).length ? `<h5>Качество данных</h5><ul>${r.data.warnings.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` : ""}
      <h5>Профессиональные метрики</h5><table class="table">
        ${row(term("CAGR", "cagr"), pct(m.cagr_pct))}${row(term("Коэффициент Шарпа", "sharpe"), fmt(m.sharpe))}
        ${row("Волатильность, годовая", pct(m.volatility_pct, false))}${row("Profit factor", fmt(m.profit_factor))}
        ${row("Время в позиции", pct(m.exposure_pct, false))}${row("Комиссии", rub(m.commissions_paid))}</table>
      ${d.regimes ? `<h5>По режимам рынка: тренд цены за 60 дней</h5><table class="table">${d.regimes.map((x) =>
        `<tr><td>${x.regime}</td><td class="num">${x.days} дн.</td><td class="num ${negc(x.strategy_pct)}">${pct(x.strategy_pct)}</td></tr>`).join("")}</table>` : ""}
      ${(d.sensitivity || []).length ? `<h5>Соседние параметры</h5><table class="table">${d.sensitivity.map((x) =>
        `<tr><td>${esc(Object.entries(x.params).map(([k, val]) => `${k}: ${fmt(val, Number.isInteger(val) ? 0 : 1)}`).join(", "))}${x.is_current ? " <span class='muted small'>текущие</span>" : ""}</td>
        <td class="num ${negc(x.return_pct)}">${pct(x.return_pct)}</td></tr>`).join("")}</table>` : ""}
      <h5>${term("Сверка", "verify")} двух движков: ${v.status === "OK" ? "совпали" : "<span class='neg'>не совпали</span>"}</h5>
      <table class="table"><tr><th>Проверка</th><th class="num">Основной</th><th class="num">Контрольный</th></tr>${v.checks.map((c) =>
        `<tr class="${c.ok ? "" : "bad"}"><td>${esc(c.name)}</td><td class="num">${esc(c.main)}</td><td class="num">${esc(c.reference)}</td></tr>`).join("")}</table>
      <h5>Трасса последнего запроса</h5><div id="traceBox" class="muted small">загружаю…</div>
      <div class="links"><a href="/api/runs/${r.run_id}/report.html" target="_blank" rel="noopener">Отчёт HTML</a>
        <a href="/api/runs/${r.run_id}/trades.csv">Сделки CSV</a></div>`;
  }

  // =================== настройки: окно с разделами ===================
  const SECTIONS = [["general", "Общие"], ["look", "Внешний вид"], ["answers", "Ответы"], ["model", "Модель и лимиты"], ["data", "Данные"], ["keys", "Сочетания клавиш"]];
  const row = (a, b) => `<div class="set-row"><span>${a}</span><span>${b}</span></div>`;
  const sw = (id, on, label, note = "") => `<div class="set-row"><span class="set-l">${label}${note ? `<small>${note}</small>` : ""}</span>
    <button class="switch" type="button" role="switch" id="${id}" aria-checked="${on}" aria-label="${label}"></button></div>`;
  const seg = (name, cur, opts) => `<div class="seg3" role="group">${opts.map(([k, t]) =>
    `<button type="button" data-${name}="${k}" aria-pressed="${k === cur}">${t}</button>`).join("")}</div>`;
  const num = (v) => Number(v || 0).toLocaleString("ru-RU").replace(/\s/g, " ");
  const flip = (e) => { const on = e.currentTarget.getAttribute("aria-checked") !== "true"; e.currentTarget.setAttribute("aria-checked", String(on)); return on; };
  const usageLine = (u) => `${num(u.tokens)} токенов, ${fmt(u.cost_rub, 2)} ₽`;
  let llmCheck = null;                                   // результат последней проверки связи, живёт до перезагрузки страницы
  async function checkLlm() { llmCheck = { ...(await api("/api/llm/check", { method: "POST" })), at: Date.now() }; return llmCheck; }
  const checkText = (k) => !k ? "" : k.ok ? `<span class="ok">Работает</span>, ответ за ${fmt(k.latency_ms / 1000, 1)} с`
    : `<span class="neg">Нет связи.</span> ${esc(k.text || "")}`;
  const balanceText = (k) => k && k.ok && k.balance_tokens != null ? `${num(k.balance_tokens)} токенов`
    : `<a href="https://developers.sber.ru/studio" target="_blank" rel="noopener">В личном кабинете</a>`;

  const RENDER = {
    general: () => `${sw("swSignal", prefs.signal, "Помощник Signal", "Персонаж на стартовом экране и рядом с ответами")}
      ${sw("swSimple", prefs.simple, "Подсказки к терминам", "Пунктир под терминами, наведите курсор, чтобы прочитать")}`,
    look: () => {
      const mode = localStorage.getItem("msa_theme") || "system", m = root.dataset.theme, c = themeColors(m), own = "accent" in look.colors[m];
      const th = (k) => { const t = themeColors(k); return `<span class="th th-${k}" style="--tb:${t.bg};--tf:${t.fg};--ta:${t.accent}"><i class="th-side"></i><i class="th-l a"></i><i class="th-l b"></i><i class="th-l c"></i><i class="th-l d"></i><i class="th-box"></i></span>`; };
      const chip = (k, label) => `<span class="col-chip"><button type="button" class="col-sw" data-col="${k}" data-label="${label}" style="background:${c[k]}" aria-label="${label}, открыть палитру"></button>
        <input type="text" data-hex="${k}" value="${c[k].toUpperCase()}" maxlength="7" spellcheck="false" autocomplete="off" aria-label="${label}, код цвета"></span>`;
      const sel = (id, label, cur, opts) => `<span class="look-sel"><select id="${id}" aria-label="${label}">${opts.map(([k, t]) => `<option value="${k}"${k === cur ? " selected" : ""}>${t}</option>`).join("")}</select><svg><use href="#i-chev"/></svg></span>`;
      return `<div class="look-card look-mode"><span>Режим</span><div class="modes" role="group" aria-label="Режим">${[["system", "Как в системе"], ["light", "Светлый"], ["dark", "Тёмный"]].map(([k, t]) =>
          `<button type="button" class="mode-th" data-theme-set="${k}" aria-pressed="${k === mode}" aria-label="${t}" title="${t}">${k === "system" ? th("light") + th("dark") : th(k)}</button>`).join("")}</div></div>
        <div class="look-card">
          <div class="set-row"><span>Тема</span><span class="look-ctl">
            <button type="button" class="look-ic" id="thImport" title="Вставить код темы" aria-label="Вставить код темы"><svg><use href="#i-down"/></svg></button>
            <button type="button" class="look-ic" id="thCopy" title="Скопировать код темы" aria-label="Скопировать код темы"><svg><use href="#i-copy"/></svg></button>
            ${sel("thPreset", "Тема", look.preset, Object.entries(THEMES).map(([k, t]) => [k, t.name]))}</span></div>
          <div class="set-row" id="thPasteRow" hidden><input class="look-paste" id="thPaste" placeholder="Вставьте код темы и нажмите Enter" spellcheck="false" autocomplete="off" aria-label="Код темы"></div>
          <div class="set-row"><span>Акцентный цвет</span><span class="look-ctl">${sel("acMode", "Акцентный цвет", own ? "own" : "theme", [["theme", "Из темы"], ["own", "Свой цвет"]])}${chip("accent", "Акцентный цвет")}</span></div>
          <div class="set-row"><span>Фон</span>${chip("bg", "Фон")}</div>
          <div class="set-row"><span>Цвет текста</span>${chip("fg", "Цвет текста")}</div>
        </div>
        <p class="set-note" id="lookNote"></p>`;
    },
    answers: (c) => `${row(`<span class="set-l">Объяснение результата<small>Коротко: два-три предложения. Подробно: разбор по годам и режимам рынка</small></span>`,
        seg("style-set", prefs.style, [["short", "Коротко"], ["full", "Подробно"]]))}
      <div class="set-row"><span class="set-l">Капитал по умолчанию<small id="cashNote">Берётся, если сумма не названа в запросе</small></span>
        <span class="cash-set"><input id="cashInput" inputmode="numeric" autocomplete="off" aria-label="Капитал по умолчанию, рублей"
          value="${num(prefs.cash || (c && c.default_cash) || 100000)}"> ₽</span></div>`,
    model: (c, u) => {
      if (!c) return `<p class="muted">Сведения о модели недоступны: сервер не ответил.</p>`;
      const l = c.limits || {};
      return `${row("Модель", esc(c.model || c.provider))}
        ${row("Ключ API", c.llm_ready ? '<span class="ok">Найден</span>' : '<span class="neg">Не найден в .env</span>')}
        <div class="set-row"><span class="set-l">Связь<small id="checkOut">${checkText(llmCheck) || "Проверка не тратит токены"}</small></span>
          <button class="btn-ghost btn-sm" type="button" id="checkBtn">Проверить</button></div>
        ${row("Остаток у GigaChat", `<span id="balOut">${llmCheck ? balanceText(llmCheck) : "Появится после проверки связи"}</span>`)}
        <div class="set-h">Расход</div>
        ${u ? row("Сегодня", usageLine(u.today)) + row("За всё время", usageLine(u.total)) : row("Расход", "нет данных")}
        ${row("Лимит на один запрос", `до ${l.max_steps} шагов, ${l.max_llm_calls} обращений к модели, ${fmt(l.max_cost_rub_per_request, 0)} ₽`)}
        <details class="set-tech"><summary>Подробнее</summary>
          ${row("Провайдер", esc(c.provider))}
          ${row("Запасные провайдеры", esc((c.fallback || []).slice(1).join(", ") || "нет"))}
          ${row("Котировки", c.data_mode === "demo" ? "Демонстрационные (флаг --demo)" : "MOEX ISS с кешем")}
          ${row("Вызовов инструментов на запрос", l.max_tool_calls)}
          <p class="muted small" style="margin-top:12px">Меняется в configs/providers.yaml и configs/settings.yaml, ключи лежат в .env и здесь не показываются.</p>
        </details>`;
    },
    data: () => `<div class="set-row"><span class="set-l">История запусков<small>Избранное останется. Действие нельзя отменить</small></span>
        <button class="btn-ghost btn-sm" type="button" id="clearBtn">Очистить историю</button></div>`,
    keys: () => `<dl class="keys"><dt><kbd class="mod">⌘</kbd><kbd>K</kbd></dt><dd>Новый анализ</dd>
      <dt><kbd>/</kbd></dt><dd>К полю ввода</dd>
      <dt><kbd>Esc</kbd></dt><dd>Закрыть окно, меню или свернуть отчёт</dd>
      <dt><kbd>←</kbd><kbd>→</kbd></dt><dd>Предыдущая и следующая сделка в открытом отчёте</dd></dl>`,
  };

  function wireSection(id, body) {
    if (id === "general") {
      $("swSimple").addEventListener("click", (e) => { prefs.simple = flip(e); localStorage.setItem("msa_simple", prefs.simple ? "1" : "0"); root.classList.toggle("simple", prefs.simple); });
      $("swSignal").addEventListener("click", (e) => { prefs.signal = flip(e); localStorage.setItem("msa_signal", prefs.signal ? "1" : "0"); root.classList.toggle("no-signal", !prefs.signal); });
    }
    if (id === "look") {
      const redraw = (keep) => { if (!keep) closeCp(); body.innerHTML = RENDER.look(); wireSection("look", body); };
      const m = root.dataset.theme;
      const note = () => {
        const c = themeColors(m), k = contrast(c.fg, c.bg), changed = Object.keys(look.colors.dark).length + Object.keys(look.colors.light).length > 0;
        $("lookNote").classList.toggle("bad", k < 4.5);
        $("lookNote").innerHTML = (k < 4.5 ? `Текст плохо читается на таком фоне: контраст ${fmt(k, 1)} при норме 4,5. ` : "")
          + (changed ? `<button type="button" class="look-reset" id="lookReset">Вернуть цвета темы</button>` : "");
        if (changed) $("lookReset").addEventListener("click", () => { look.colors = { dark: {}, light: {} }; saveLook(); applyTheme(); redraw(); });
      };
      note();
      body.querySelectorAll("[data-theme-set]").forEach((b) => b.addEventListener("click", () => { setTheme(b.dataset.themeSet); redraw(); }));
      $("thPreset").addEventListener("change", (e) => { look.preset = e.target.value; look.colors = { dark: {}, light: {} }; saveLook(); applyTheme(); redraw(); });
      $("acMode").addEventListener("change", (e) => {
        if (e.target.value === "own") look.colors[m].accent = themeColors(m).accent; else delete look.colors[m].accent;
        saveLook(); applyTheme(); redraw(); });
      const setCol = (k, v, final) => {
        look.colors[m][k] = v.toLowerCase();
        if (look.colors[m][k] === THEMES[look.preset][m][k] && k !== "accent") delete look.colors[m][k];
        saveLook();
        if (final) { applyTheme(); redraw(true); return; }
        applyColors(); body.querySelector(`[data-hex="${k}"]`).value = v.toUpperCase(); body.querySelector(`[data-col="${k}"]`).style.background = v; if (k === "accent") $("acMode").value = "own"; note();
      };
      body.querySelectorAll("[data-col]").forEach((b) => b.addEventListener("click", () => {
        const k = b.dataset.col; if (cp && cp.k === k) { closeCp(); return; }
        openCp(b, k, themeColors(m)[k], b.dataset.label, (c, final) => setCol(k, c, final)); }));
      body.querySelectorAll("[data-hex]").forEach((i) => i.addEventListener("change", () => {
        const v = i.value.trim().replace(/^#?/, "#");
        if (!hx(v)) { toast("Нужен код цвета вида #1A2B3C"); redraw(); return; }
        closeCp(); setCol(i.dataset.hex, v, true); }));
      $("thCopy").addEventListener("click", async () => {
        const code = JSON.stringify({ msa_theme: 1, preset: look.preset, dark: themeColors("dark"), light: themeColors("light") });
        try { await navigator.clipboard.writeText(code); toast("Код темы скопирован"); } catch (_) { toast("Браузер не дал доступ к буферу обмена"); } });
      $("thImport").addEventListener("click", () => { const r = $("thPasteRow"); r.hidden = !r.hidden; if (!r.hidden) $("thPaste").focus(); });
      $("thPaste").addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        if (!importLook(e.target.value)) { toast("Это не код темы"); return; }
        applyTheme(); redraw(); toast("Тема применена"); });
    }
    if (id === "answers") {
      body.querySelectorAll("[data-style-set]").forEach((b) => b.addEventListener("click", () => {
        body.querySelectorAll("[data-style-set]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        prefs.style = b.dataset.styleSet; localStorage.setItem("msa_style", prefs.style); }));
      const ci = $("cashInput"), cn = $("cashNote"), cnText = cn.textContent;
      ci.addEventListener("change", () => {
        const v = Math.round(+ci.value.replace(/[^\d]/g, ""));
        if (!v || v < 1000 || v > 1e9) { cn.textContent = "Нужна сумма от 1 000 до 1 000 000 000 ₽"; cn.classList.add("bad"); return; }
        cn.textContent = cnText; cn.classList.remove("bad");
        prefs.cash = v; localStorage.setItem("msa_cash", String(v)); ci.value = num(v); toast("Капитал по умолчанию: " + rub(v));
      });
      ci.addEventListener("input", () => { cn.textContent = cnText; cn.classList.remove("bad"); });
    }
    if (id === "model" && $("checkBtn")) $("checkBtn").addEventListener("click", async (e) => {
      const b = e.currentTarget; b.disabled = true; $("checkOut").textContent = "Проверяю…";
      try { const k = await checkLlm(); $("checkOut").innerHTML = checkText(k); $("balOut").innerHTML = balanceText(k); }
      catch (err) { $("checkOut").textContent = "Проверка не удалась: " + err.message; }
      b.disabled = false;
    });
    if (id === "data") $("clearBtn").addEventListener("click", async () => {
      try {
        if (await humanAction(() => api(`/api/history/clear?session_id=${sid()}`, { method: "POST" }))) { state.runs.clear(); await loadSide(); closeDrawers(); newAnalysis(); }
      } catch (err) { toast("Не удалось: " + err.message); }
    });
  }

  async function openSettings(section = "general") {
    closeMobileSide(); closeUsage(); openDrawer("settings");
    $("setNav").innerHTML = SECTIONS.map(([k, t]) => `<button type="button" role="tab" data-sec="${k}" aria-selected="${k === section}">${t}</button>`).join("");
    let c = null, u = null;
    const show = (id) => {
      $("setNav").querySelectorAll("[data-sec]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.sec === id)));
      closeCp(); $("setTitle").textContent = SECTIONS.find(([k]) => k === id)[1];
      $("settingsBody").innerHTML = RENDER[id](c, u); wireSection(id, $("settingsBody"));
    };
    $("setNav").onclick = (e) => { const b = e.target.closest("[data-sec]"); if (b) show(b.dataset.sec); };
    show(section);
    try { [c, u] = await Promise.all([api("/api/config"), api("/api/usage")]); } catch (_) {}
    const cur = $("setNav").querySelector('[aria-selected="true"]');
    if (!$("settings").hidden && cur && ["answers", "model"].includes(cur.dataset.sec)) show(cur.dataset.sec);
  }
  $("settingsBtn").addEventListener("click", () => openSettings());

  // =================== расход: окошко у боковой панели ===================
  function closeUsage() { $("usagePop").hidden = true; $("usageBtn").setAttribute("aria-expanded", "false"); }
  $("usageBtn").addEventListener("click", async (e) => {
    e.stopPropagation();
    const pop = $("usagePop");
    if (!pop.hidden) { closeUsage(); return; }
    if (!isMobile()) pop.style.bottom = (window.innerHeight - e.currentTarget.getBoundingClientRect().top + 6) + "px";
    pop.innerHTML = `<p class="muted small">Загружаю…</p>`; pop.hidden = false; $("usageBtn").setAttribute("aria-expanded", "true");
    let u; try { u = await api("/api/usage"); } catch (_) { pop.innerHTML = `<p class="muted small">Расход недоступен</p>`; return; }
    const draw = () => { pop.innerHTML = `<div class="up-src">Модель: ${esc(u.model || u.provider)}</div>
      <div class="up-row"><span>Сегодня</span><b>${usageLine(u.today)}</b></div>
      <div class="up-row"><span>За всё время</span><b>${usageLine(u.total)}</b></div>
      <div class="up-row"><span>Остаток</span><b id="upBal">${llmCheck ? balanceText(llmCheck) : "…"}</b></div>
      <button type="button" class="up-more" id="upMore">Модель и лимиты</button>`;
      $("upMore").addEventListener("click", () => openSettings("model")); };
    draw();
    if (!llmCheck || Date.now() - llmCheck.at > 300000) { try { await checkLlm(); } catch (_) { llmCheck = { ok: false, at: Date.now() }; } if (!pop.hidden) draw(); }
  });
  document.addEventListener("click", (e) => { if (!$("usagePop").hidden && !e.target.closest(".usage-wrap")) closeUsage(); });

  // =================== тема ===================
  // Набор CBR без правок живёт в app.css. Любой другой набор или свой цвет: токены считаются из трёх цветов и ставятся поверх.
  const POS = { dark: "#5fd899", light: "#0b7a48" };      // рост остаётся зелёным в наборах, где акцент не зелёный
  const THEMES = {
    cbr: { name: "CBR", dark: { accent: "#7affb4", bg: "#000a06", fg: "#ffffff" }, light: { accent: "#0b7a48", bg: "#f6f8f7", fg: "#0a1a14" } },
    graphite: { name: "Графит", pos: POS, dark: { accent: "#e6e6e6", bg: "#0d0d0d", fg: "#f3f3f3" }, light: { accent: "#0d0d0d", bg: "#f9f9f9", fg: "#0d0d0d" } },
  };
  const hx = (h) => { const m = /^#([0-9a-f]{6})$/i.exec(String(h).trim()); return m ? [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)) : null; };
  const mix = (a, b, t) => { const x = hx(a), y = hx(b); return "#" + x.map((v, i) => Math.round(v + (y[i] - v) * t).toString(16).padStart(2, "0")).join(""); };
  const alpha = (h, a) => `rgba(${hx(h).join(", ")}, ${a})`;
  const lum = (h) => { const [r, g, b] = hx(h).map((v) => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return .2126 * r + .7152 * g + .0722 * b; };
  const contrast = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
  const look = { preset: "cbr", colors: { dark: {}, light: {} } };
  const cleanColors = (o, base) => { const r = {}; ["accent", "bg", "fg"].forEach((k) => { if (o && hx(o[k]) && (!base || o[k].toLowerCase() !== base[k])) r[k] = o[k].toLowerCase(); }); return r; };
  try {
    const p = localStorage.getItem("msa_preset"), c = JSON.parse(localStorage.getItem("msa_colors") || "{}");
    if (THEMES[p]) { look.preset = p; look.colors = { dark: cleanColors(c.dark), light: cleanColors(c.light) }; }   // удалённый набор: возврат к CBR без своих цветов
  } catch (_) {}
  const themeColors = (mode) => ({ ...THEMES[look.preset][mode], ...look.colors[mode] });
  function themeVars(mode) {
    if (look.preset === "cbr" && !Object.keys(look.colors[mode]).length) return null;
    const c = themeColors(mode), dark = lum(c.bg) < .3, t = THEMES[look.preset];
    let ink = c.accent;                                  // акцент для текста и линий: подтягиваем к цвету текста, пока не станет читаемым
    for (let i = 0; i < 12 && contrast(ink, c.bg) < 4.5; i++) ink = mix(ink, c.fg, .2);
    const soft = mix(c.fg, c.bg, .12), muted = mix(c.fg, c.bg, .4);
    const v = { "--bg": c.bg, "--card": dark ? mix(c.bg, c.fg, .06) : mix(c.bg, "#ffffff", .75), "--elev": dark ? mix(c.bg, c.fg, .09) : mix(c.bg, "#ffffff", .9),
      "--text": c.fg, "--text-soft": soft, "--muted": muted, "--faint": mix(c.fg, c.bg, .55),
      "--line": alpha(c.fg, .09), "--line-strong": alpha(c.fg, .17), "--line-focus": alpha(c.fg, .32), "--hover": alpha(c.fg, .04), "--hover-strong": alpha(c.fg, .07),
      "--mint": c.accent, "--on-mint": contrast(c.accent, "#0d0d0d") > contrast(c.accent, "#ffffff") ? "#0d0d0d" : "#ffffff",
      "--accent": ink, "--mint-soft": alpha(c.accent, dark ? .1 : .14), "--accent-line": alpha(ink, .24),
      "--ch-strategy": ink, "--ch-bench": muted, "--ch-grid": alpha(c.fg, .05), "--ch-start": alpha(c.fg, .22), "--ch-ov1": alpha(c.fg, .5),
      "--ch-level": alpha(c.fg, .28), "--ch-hl": alpha(ink, .08), "--ch-vol": alpha(c.fg, .2), "--ch-price": soft };
    if (t.pos && !("accent" in look.colors[mode])) v["--pos"] = t.pos[dark ? "dark" : "light"];
    return v;
  }
  function applyColors() {
    const all = { dark: themeVars("dark"), light: themeVars("light") }, v = all[root.dataset.theme] || {};
    [...root.style].filter((k) => k.startsWith("--")).forEach((k) => root.style.removeProperty(k));
    Object.keys(v).forEach((k) => root.style.setProperty(k, v[k]));
    try { if (all.dark || all.light) localStorage.setItem("msa_vars", JSON.stringify(all)); else localStorage.removeItem("msa_vars"); } catch (_) {}
  }
  const hsv2hex = (h, s, v) => { const f = (n) => { const k = (n + h / 60) % 6; return Math.round(255 * (v - v * s * Math.max(0, Math.min(k, 4 - k, 1)))).toString(16).padStart(2, "0"); }; return "#" + f(5) + f(3) + f(1); };
  const hex2hsv = (hex) => { const [r, g, b] = hx(hex).map((x) => x / 255), mx = Math.max(r, g, b), dd = mx - Math.min(r, g, b);
    let h = 0; if (dd) h = mx === r ? ((g - b) / dd + 6) % 6 : mx === g ? (b - r) / dd + 2 : (r - g) / dd + 4;
    return { h: h * 60, s: mx ? dd / mx : 0, v: mx }; };
  let cp = null;                                         // открытая палитра: поле насыщенности и яркости, под ним ползунок тона
  function closeCp() {
    if (!cp) return;
    cp.el.remove(); document.removeEventListener("pointerdown", cp.out, true); document.removeEventListener("keydown", cp.key, true); cp = null;
  }
  function openCp(anchor, k, hex, label, onPick) {
    closeCp();
    const win = $("settings"), el = document.createElement("div"), st = hex2hsv(hex), lim = (x) => Math.max(0, Math.min(1, x));
    el.className = "cp";
    el.innerHTML = `<div class="cp-sv" tabindex="0" role="slider" aria-label="${label}: насыщенность и яркость, стрелки"><i></i></div>
      <div class="cp-h" tabindex="0" role="slider" aria-label="${label}: тон" aria-valuemin="0" aria-valuemax="360"><i></i></div>`;
    win.appendChild(el);
    const sv = el.querySelector(".cp-sv"), hu = el.querySelector(".cp-h");
    const paint = () => {
      const c = hsv2hex(st.h, st.s, st.v), pure = hsv2hex(st.h, 1, 1);
      sv.style.setProperty("--hue", pure); sv.firstChild.style.cssText = `left:${st.s * 100}%;top:${(1 - st.v) * 100}%;background:${c}`;
      hu.firstChild.style.cssText = `left:${st.h / 3.6}%;background:${pure}`;
      hu.setAttribute("aria-valuenow", Math.round(st.h)); sv.setAttribute("aria-valuetext", c.toUpperCase()); return c;
    };
    const drag = (node, set) => node.addEventListener("pointerdown", (e) => {
      e.preventDefault(); node.focus(); node.setPointerCapture(e.pointerId);
      const mv = (ev) => { const r = node.getBoundingClientRect(); set(lim((ev.clientX - r.left) / r.width), lim((ev.clientY - r.top) / r.height)); onPick(paint(), false); };
      const up = () => { node.removeEventListener("pointermove", mv); node.removeEventListener("pointerup", up); node.removeEventListener("pointercancel", up); onPick(paint(), true); };
      mv(e); node.addEventListener("pointermove", mv); node.addEventListener("pointerup", up); node.addEventListener("pointercancel", up);
    });
    drag(sv, (x, y) => { st.s = x; st.v = 1 - y; }); drag(hu, (x) => { st.h = x * 360; });
    const arrows = (node, fn) => node.addEventListener("keydown", (e) => {
      const dx = { ArrowLeft: -1, ArrowRight: 1 }[e.key] || 0, dy = { ArrowDown: -1, ArrowUp: 1 }[e.key] || 0;
      if (!dx && !dy) return; e.preventDefault(); fn(dx, dy, e.shiftKey ? 5 : 1); onPick(paint(), true); });
    arrows(sv, (dx, dy, n) => { st.s = lim(st.s + dx * .02 * n); st.v = lim(st.v + dy * .02 * n); });
    arrows(hu, (dx, dy, n) => { st.h = Math.max(0, Math.min(360, st.h + (dx || dy) * 3 * n)); });
    paint();
    const a = anchor.getBoundingClientRect(), w = win.getBoundingClientRect();
    let top = a.bottom - w.top + 8; if (top + el.offsetHeight > w.height - 8) top = a.top - w.top - el.offsetHeight - 8;
    el.style.top = Math.max(8, top) + "px"; el.style.left = Math.max(8, Math.min(a.right - w.left - el.offsetWidth, w.width - el.offsetWidth - 8)) + "px";
    cp = { el, k, out: (e) => { if (!el.contains(e.target) && !e.target.closest("[data-col]")) closeCp(); },
      key: (e) => { if (e.key === "Escape") { e.stopPropagation(); closeCp(); } } };
    document.addEventListener("pointerdown", cp.out, true); document.addEventListener("keydown", cp.key, true);
    sv.focus({ preventScroll: true });
  }
  function saveLook() { try { localStorage.setItem("msa_preset", look.preset); localStorage.setItem("msa_colors", JSON.stringify(look.colors)); } catch (_) {} }
  function importLook(text) {
    let o; try { o = JSON.parse(text); } catch (_) { return false; }
    if (!o || o.msa_theme !== 1 || !THEMES[o.preset]) return false;
    const ok = (c) => c && ["accent", "bg", "fg"].every((k) => hx(c[k]));
    if (!ok(o.dark) || !ok(o.light)) return false;
    look.preset = o.preset; look.colors = { dark: cleanColors(o.dark, THEMES[o.preset].dark), light: cleanColors(o.light, THEMES[o.preset].light) };
    saveLook(); return true;
  }
  const sysLight = window.matchMedia("(prefers-color-scheme: light)");
  function applyTheme() {
    const t = localStorage.getItem("msa_theme") || "system";
    root.dataset.theme = t === "system" ? (sysLight.matches ? "light" : "dark") : t;
    applyColors(); readTheme();
    log.querySelectorAll(".ap:not(.collapsed)").forEach((el) => {
      plotEquity(el, el._r);
      if (!el.querySelector('[data-pane="price"]').hidden) plotPrice(el, el._r, el._pv || "price");
    });
  }
  function setTheme(t) { try { localStorage.setItem("msa_theme", t); } catch (_) {} applyTheme(); }
  sysLight.addEventListener("change", () => { if ((localStorage.getItem("msa_theme") || "system") === "system") applyTheme(); });

  // =================== восстановление диалога ===================
  async function restore(focusRun) {
    if (!state.sessionId) return false;
    let s;
    try { s = await api(`/api/sessions/${state.sessionId}/messages`); } catch (_) { state.sessionId = null; localStorage.removeItem("msa_session"); return false; }
    if (!s.messages.length) return false;
    showIntro(false);
    state.pending = new Set((s.pending || []).map((p) => p.id));
    const msgs = s.messages.slice(-40);
    const withRun = msgs.filter((m) => m.role === "assistant" && (m.meta.blocks || []).some((b) => b.type === "result" && !b.tab));
    const target = focusRun ? withRun.find((m) => m.meta.blocks.some((b) => b.run_id === focusRun)) : withRun[withRun.length - 1];
    let prevUser = "";
    for (const m of msgs) {
      if (m.role === "user") { addUser(m.content); prevUser = m.content; continue; }
      const d = agentShell();
      renderReply(d, { text: m.content, blocks: m.meta.blocks || [], intent: m.meta.intent, flags: m.meta.flags || [],
                       suggestions: m === target ? m.meta.suggestions : [] }, { restoring: true, collapsed: m !== target, asked: prevUser });
    }
    const last = msgs.filter((m) => m.role === "assistant").pop();
    if (last && last.meta) { setChips(last.meta.suggestions); state.lastTrace = last.meta.trace_id; }
    await Promise.all([...log.querySelectorAll(".msg")].map((m) => m._ready).filter(Boolean));
    if (!log.querySelector(".ap:not(.collapsed)")) { const all = log.querySelectorAll(".ap"); if (all.length) expandPanel(all[all.length - 1]); }
    const focusEl = focusRun && log.querySelector(`.ap[data-run="${focusRun}"]`);
    if (focusEl) { setActive(focusRun); focusEl.closest(".msg").scrollIntoView({ block: "start" }); }
    else toBottom(false);
    return !focusRun || !!focusEl;
  }

  // =================== старт ===================
  Signal.create($("introSignal"), { size: isMobile() ? 110 : 140 });
  api("/api/config").then((c) => { if (c && c.llm_ready === false) $("llmOff").hidden = false; }).catch(() => {});
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { readTheme(); resizePlots(); syncToggle(); }, 180); });
  syncToggle();
  if (isMobile()) input.placeholder = "Опишите стратегию или задайте вопрос…";
  loadSide(); restore().then(() => { if (!isMobile()) input.focus(); });
})();
