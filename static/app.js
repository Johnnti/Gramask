"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const categories = {all:"All topics",dining:"Dining",library:"Library",transport:"Transport",events:"Events",campus:"Campus"};
  const categoryIcons = {dining:"dining",library:"book",transport:"transport",events:"events",campus:"campus"};
  const requestedSlot = new URLSearchParams(location.search).get("session") || "";
  const sessionSlot = !requestedSlot || /^[a-f0-9]{32}$/.test(requestedSlot) ? requestedSlot : "invalid";
  const authScope = sessionSlot || "default";
  const authStorageKey = `gramask_auth_changed_${authScope}`;
  const state = {
    user:null, csrf:"", config:null, stats:null, queries:[], inbox:[], contributions:[],
    facts:{static:[],live:[]}, notifications:[], view:"ask", authMode:"login",
    filters:{queries:"all",community:"all",live:"all",knowledge:"all"},
    search:{queries:"",community:"",live:"",knowledge:""},
    loaded:false, authChecked:false, refreshing:false, refreshQueued:false,
    requestVersion:0, seenNotifications:new Set(), notificationsInitialized:false,
    answerDrafts:new Map(), busyAnswers:new Set(), communityTab:"inbox",
    demoQuestion:null, askBusy:false, authBusy:false, demoBusy:false, browserAlerts:false,
    students:[], studentsBusy:false, clockOffset:0,
  };
  let pollTimer, countdownTimer;
  let hiddenPollTicks = 0;
  const authChannel = "BroadcastChannel" in window ? new BroadcastChannel(`gramask-auth-${authScope}`) : null;
  let authAction = null;
  const rendered = new Map();
  const timeFormatter = new Intl.RelativeTimeFormat(undefined, {numeric:"auto"});
  const now = () => Date.now() / 1000 + state.clockOffset;
  function countdownLabel(deadline) {
    const seconds = Math.max(0, Math.ceil(deadline - now()));
    return seconds ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2,"0")} left` : "Time’s up · finalizing";
  }
  function updateCountdowns() {
    let expired = false;
    document.querySelectorAll("[data-deadline]").forEach(node => {
      const deadline = Number(node.dataset.deadline);
      node.textContent = countdownLabel(deadline);
      if (deadline <= now()) {
        expired = true;
        node.closest(".inbox-card")?.querySelectorAll("button,textarea").forEach(control => { control.disabled = true; });
      }
    });
    if (expired && !document.hidden && !state.refreshing) void refresh();
  }
  function reportSummaryHTML(summary) {
    if (!summary.total) return '<p class="answer-copy">No answers received before the deadline.</p>';
    const opening = summary.closed_by === "responses" ? `All ${summary.total} students responded.` : `Time is up. ${summary.total} of ${summary.expected} students responded.`;
    const largest = Math.max(...summary.answers.map(answer => answer.count));
    const leaders = summary.answers.filter(answer => answer.count === largest);
    const lead = leaders.length === 1 ? `Most reported: ${leaders[0].value}.` : "Responses are tied; no single answer leads.";
    return `<p class="answer-copy">${esc(opening)} ${esc(lead)}</p><table class="response-breakdown"><caption>Share of the ${summary.total} response${summary.total === 1 ? "" : "s"} received</caption><thead><tr><th scope="col">Response</th><th scope="col">Reports</th><th scope="col">Share</th></tr></thead><tbody>${summary.answers.map(answer => `<tr><th scope="row">${esc(answer.value)}</th><td>${esc(answer.count)} / ${esc(summary.total)}</td><td>${(answer.share * 100).toFixed(1).replace(/\.0$/,"")}%</td></tr>`).join("")}</tbody></table>`;
  }
  function relativeTime(seconds) {
    if (!seconds) return "Just now";
    const diff = seconds - now();
    if (Math.abs(diff) < 45) return "Just now";
    for (const [unit,size] of [["day",86400],["hour",3600],["minute",60]]) {
      if (Math.abs(diff) >= size || unit === "minute") return timeFormatter.format(Math.round(diff / size), unit);
    }
  }
  function expiryLabel(seconds) {
    if (!seconds) return "Fresh community answer";
    const remaining = seconds - now();
    if (remaining <= 0) return "Expired · ask again for an update";
    if (remaining < 60) return "Expires in under a minute";
    if (remaining < 3600) return `Expires in ${Math.ceil(remaining / 60)} min`;
    return `Expires in ${Math.floor(remaining / 3600)}h ${Math.floor(remaining % 3600 / 60)}m`;
  }
  function dateTitle(seconds) { return seconds ? new Date(seconds * 1000).toLocaleString() : ""; }
  function safeURL(value) {
    try { const url = new URL(value); return ["https:","http:"].includes(url.protocol) ? url.href : null; } catch { return null; }
  }
  function category(value) { return categories[value] ? value : "campus"; }
  function matchText(record, term) { return !term || record.toLowerCase().includes(term.toLowerCase()); }
  function setHTML(id, html) {
    // Polling must not replace a focused reply form or erase the student's draft.
    if (rendered.get(id) === html) return;
    const node = $(id);
    const focused = node.contains(document.activeElement) ? document.activeElement : null;
    const focusId = focused?.id;
    const selection = focused && "selectionStart" in focused ? [focused.selectionStart, focused.selectionEnd] : null;
    node.innerHTML = html;
    rendered.set(id, html);
    if (focusId && $(focusId)) {
      $(focusId).focus({preventScroll:true});
      if (selection) { try { $(focusId).setSelectionRange(...selection); } catch { /* unsupported input type */ } }
    }
  }
  function emptyState(symbol, title, description, action = "") {
    return `<div class="empty-state"><span class="empty-symbol">${icon(symbol)}</span><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;
  }
  function authCTA(label = "Join the conversation") { return `<button class="button button-primary" data-open-auth="register">${esc(label)} ${icon("arrow")}</button>`; }
  function sourceBadge(q) {
    if (q.report_summary) return q.report_summary.total ? '<span class="badge badge-green">Student responses</span>' : '<span class="badge badge-muted">No answers received</span>';
    if (q.is_demo) return '<span class="badge badge-amber">Demo simulation</span>';
    if (q.status === "pending") return '<span class="badge badge-amber">Asking the community</span>';
    if (q.source === "knowledge") return '<span class="badge badge-green">Campus knowledge</span>';
    if (q.source === "crowd") return '<span class="badge badge-green">Community answer</span>';
    if (q.source === "out_of_scope") return '<span class="badge badge-muted">Outside campus topics</span>';
    if (q.status === "unanswered") return '<span class="badge badge-muted">No consensus yet</span>';
    return '<span class="badge badge-green">Answered</span>';
  }
  function errorMessage(detail, fallback) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(item => item.msg || "Please check your input.").join(" ");
    return fallback || "Something went wrong. Please try again.";
  }
  async function api(path, body, options = {}) {
    const sessionVersion = state.requestVersion;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 90000);
    try {
      const response = await fetch(path, {
        method: body === undefined ? "GET" : "POST",
        credentials:"same-origin", cache:"no-store", signal:controller.signal,
        headers:{"Accept":"application/json", ...(sessionSlot ? {"X-GramAsk-Session":sessionSlot} : {}),
          ...(body === undefined ? {} : {"Content-Type":"application/json","X-CSRF-Token":state.csrf})},
        ...(body === undefined ? {} : {body:JSON.stringify(body)}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(errorMessage(data.detail, response.status >= 500 ? "The campus service is unavailable. Please try again shortly." : response.statusText));
        error.status = response.status;
        if (response.status === 401 && state.user && !options.auth && sessionVersion === state.requestVersion) expireSession();
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("This is taking longer than expected. Check your question history before trying again.");
      if (error instanceof TypeError) throw new Error("We couldn’t connect. Check your connection and try again.");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function toast(message, isError = false) {
    const node = document.createElement("div");
    node.className = `toast${isError ? " error" : ""}`;
    node.innerHTML = `${icon(isError ? "info" : "check")}<span>${esc(message)}</span>`;
    $("toast-region").append(node);
    setTimeout(() => node.remove(), isError ? 8000 : 5000);
  }
  function showError(id, message = "") { $(id).textContent = message; $(id).hidden = !message; }
  function clearPrivateState() {
    state.user = null; state.csrf = ""; state.stats = null; state.queries = []; state.inbox = []; state.contributions = []; state.notifications = [];
    state.facts.live = []; state.demoQuestion = null; state.answerDrafts.clear(); state.seenNotifications.clear(); state.notificationsInitialized = false;
    state.students = []; state.studentsBusy = false; $("student-status").textContent = "";
    state.requestVersion++;
    state.busyAnswers.clear(); state.askBusy = false; state.demoBusy = false;
    $("ask-button").disabled = false; $("ask-button").innerHTML = `Ask GramAsk ${icon("arrow")}`; $("ask-input").readOnly = false;
    $("demo-seed").disabled = false; $("demo-reset").disabled = false;
    $("ask-input").value = ""; showError("ask-error");
    $("notification-dialog").close(); $("account-dialog").close();
  }
  function announceAuthChange() {
    authChannel?.postMessage({type:"auth-changed"});
    try { localStorage.setItem(authStorageKey,String(Date.now())); } catch { /* Poll verification also reconciles shared sessions. */ }
  }
  function applySession(auth) {
    const changed = state.user?.id !== auth?.user?.id || state.csrf !== (auth?.csrf_token || "");
    if (!changed) return false;
    clearPrivateState();
    if (auth) { state.user = auth.user; state.csrf = auth.csrf_token; }
    state.authChecked = true;
    renderAll();
    return true;
  }
  async function syncSession() {
    const version = state.requestVersion;
    try {
      const auth = await api("/api/auth/me",undefined,{auth:true,timeout:15000});
      if (version !== state.requestVersion || state.authBusy) return false;
      applySession(auth);
      return true;
    } catch (error) {
      if (version !== state.requestVersion || state.authBusy) return false;
      if (error.status === 401) { applySession(null); return true; }
      throw error;
    }
  }
  function expireSession() {
    clearPrivateState(); renderAll();
    toast("Your session has ended. Sign in again to continue.", true);
  }
  function renderAccount() {
    document.title = `${state.user?.is_demo_student ? state.user.display_name + " · " : ""}${{ask:"Ask GramAsk",community:"Community",live:"Live answers",knowledge:"Knowledge base"}[state.view]} — GramAsk`;
    const initials = state.user ? state.user.display_name.trim().split(/\s+/).slice(0,2).map(word => word[0]).join("").toUpperCase() : "G";
    $("account-avatar").textContent = initials;
    $("topbar-account").textContent = initials;
    $("profile-avatar").textContent = initials;
    $("account-name").textContent = state.user?.display_name || "Make yourself at home";
    $("account-subtitle").textContent = state.user ? `@${state.user.username}` : "Sign in or join the community";
    $("topbar-account").setAttribute("aria-label", state.user ? `Account for ${state.user.display_name}` : "Sign in or create an account");
    $("account-title").textContent = state.user?.display_name || "Your account";
    $("profile-username").textContent = state.user ? `@${state.user.username}` : "";
    $("profile-questions").textContent = state.stats?.my_questions ?? state.queries.length;
    $("profile-contributions").textContent = state.stats?.my_contributions ?? state.contributions.length;
  }
  function renderConfig() {
    const config = state.config;
    if (!config) return;
    const engine = config.engine || {};
    const statuses = {available:"Gemma 3 connected",unverified:"Connection not yet verified",degraded:"Gemma 3 unavailable",demo:"Demo simulation enabled"};
    $("engine-status").innerHTML = `<span class="status-dot"></span>${esc(statuses[engine.status] || "Connection not yet verified")}`;
    $("engine-status").dataset.status = engine.status || "unverified";
    $("engine-detail").textContent = config.demo_mode ? "Simulated engine · no live AI inference" : engine.status === "degraded" ? "Answers may be unavailable or use a labeled fallback." : `${engine.model || config.llm || "Gemma 3"} · ${engine.provider === "ollama" ? "Ollama" : "Hosted endpoint"}`;
    $("engine-detail").title = engine.last_error || "";
    $("quorum-label").textContent = `${config.quorum} student responses`;
    $("community-rule-detail").textContent = config.report_all ? ` or ${config.timeout_min} minute${config.timeout_min === 1 ? "" : "s"} before results are shared. Every response counts, including “Not sure.”` : ' and enough agreement before an answer is shared. It’s always okay to say “Not sure.”';
    $("demo-panel").hidden = !(config.demo_mode && state.user && !state.user.is_demo_student);
    $("student-panel").hidden = !(config.demo_mode && state.user);
    const student = state.user?.is_demo_student;
    $("student-panel-title").textContent = student ? `You’re signed in as ${state.user.display_name}.` : "Ask here. Answer in three student tabs.";
    $("student-panel-help").textContent = student ? "Open Questions for you to submit your answer. Live answers shows completed results. Each student gets one vote, and the final answer returns automatically to the account that asked." : "Ask a question or create a sample below. Set up the student accounts, open each link, and submit an answer in each tab. Return here to see the answer after three agreeing reports.";
    if (config.report_all) $("student-panel-help").textContent = student ? `Share Yes, No, Not sure, or a written response. Results return to the asker after ${config.quorum} responses or ${config.timeout_min} minute${config.timeout_min === 1 ? "" : "s"}.` : `Ask here, then open each student tab and submit a response. Results return here after ${config.quorum} responses or ${config.timeout_min} minute${config.timeout_min === 1 ? "" : "s"}, with the share for each answer. “Not sure” counts too.`;
    $("prepare-students").hidden = !!student;
    $("prepare-students").disabled = state.studentsBusy;
    $("prepare-students").textContent = state.students.length ? "Reconnect student accounts" : "Set up 3 student accounts";
    setHTML("student-links", student ? `<a class="button button-small button-primary" href="#community" data-student-inbox>Questions for you (${state.inbox.length}) ${icon("arrow")}</a>` : state.students.map(account => `<a class="button button-small button-outline" href="${esc(account.url)}" target="_blank" rel="noopener">Open ${esc(account.name)} ${icon("up-right")}</a>`).join(""));
    const pendingDemo = state.queries.find(query => query.is_demo && query.status === "pending" && query.crowd_question_id);
    state.demoQuestion = pendingDemo?.crowd_question_id || null;
    $("demo-answer").disabled = !state.demoQuestion || state.demoBusy;
  }
  function renderStats() {
    $("stat-open").textContent = state.stats?.open_questions ?? "—";
    $("stat-fresh").textContent = state.stats?.fresh_answers ?? "—";
    $("stat-students").textContent = state.stats?.students ?? "—";
    $("pulse-footer").textContent = state.user ? "Community activity · refreshed while you’re here" : "Sign in to see what’s happening.";
    $("community-count").textContent = state.inbox.length;
    $("community-count").hidden = !state.inbox.length;
    $("inbox-tab-count").textContent = state.inbox.length;
    $("contributions-tab-count").textContent = state.contributions.length;
  }
  function renderQueries() {
    $("question-count").textContent = state.queries.length;
    $("question-controls").hidden = !state.user || !state.queries.length;
    if (!state.authChecked) return;
    if (!state.user) {
      setHTML("questions-list", emptyState("ask", "Your next good question starts here.", "Create your account to ask about campus and keep your answers in one place.", authCTA("Make yourself at home") + '<button class="text-button" data-open-auth="login">Already here? Sign in</button>'));
      return;
    }
    const queries = state.queries.filter(q => (state.filters.queries === "all" || q.status === state.filters.queries) && matchText(`${q.text} ${q.answer || ""}`, state.search.queries));
    if (!queries.length) {
      setHTML("questions-list", emptyState("ask", state.queries.length ? "No questions match just yet." : "A good place to get curious.", state.queries.length ? "Try another search or switch the filter to see your questions." : "Ask your first campus question above. We’ll look for trusted knowledge or check with the community."));
      return;
    }
    setHTML("questions-list", queries.map(q => {
      const source = safeURL(q.source_url);
      const pending = q.status === "pending";
      const expiry = q.valid_until ? `<span class="${q.valid_until <= now() ? "expired" : ""}">${icon("clock")}${esc(expiryLabel(q.valid_until))}</span>` : "";
      const votes = q.n_votes !== undefined && q.n_votes !== null ? `<span>${icon("community")}${esc(q.n_votes)} student response${q.n_votes === 1 ? "" : "s"}</span>` : "";
      const confidence = !q.report_summary && typeof q.confidence === "number" ? `<span>${Math.round(q.confidence * 100)}% agreement</span>` : "";
      const fallback = q.engine === "fallback" || q.classification?.engine === "fallback" ? '<span class="badge badge-amber">Rules fallback · AI unavailable</span>' : "";
      return `<article class="question-card" id="query-${esc(q.id)}"><div class="question-topline">${sourceBadge(q)}<time class="question-time" title="${esc(dateTitle(q.created_at))}">${esc(relativeTime(q.created_at))}</time></div><h3>${esc(q.text)}</h3>${pending ? `<div class="answer-pending">${icon("clock")}Checking with students. <span class="deadline" data-deadline="${esc(q.closes_at)}">${esc(countdownLabel(q.closes_at))}</span></div>` : q.report_summary ? reportSummaryHTML(q.report_summary) : `<p class="answer-copy">${esc(q.answer || "There weren’t enough matching responses. Try asking again later.")}</p>`}<div class="answer-metadata">${source ? `<a href="${esc(source)}" target="_blank" rel="noopener noreferrer">${icon("up-right")}View source</a>` : ""}${votes}${confidence}${expiry}${fallback}${pending ? `<span>${icon("shield")}Your name isn’t shared with the question.</span>` : ""}${q.status === "unanswered" || (q.valid_until && q.valid_until <= now()) ? `<button class="text-button" data-reask="${esc(q.id)}">Ask again ${icon("arrow")}</button>` : ""}</div></article>`;
    }).join(""));
  }
  function renderCategories(view) {
    setHTML(`${view}-categories`, Object.entries(categories).map(([key,label]) => `<button data-category="${key}" data-category-view="${view}" aria-pressed="${state.filters[view] === key}" class="${state.filters[view] === key ? "selected" : ""}">${esc(label)}</button>`).join(""));
  }
  function renderInbox() {
    renderCategories("community");
    if (!state.user) { setHTML("inbox-list", emptyState("community", "You know more than you think.", "Join your campus community to see open questions and help a fellow Tiger.", authCTA())); return; }
    const inbox = state.inbox.filter(q => (state.filters.community === "all" || category(q.category) === state.filters.community) && matchText(q.prompt, state.search.community));
    if (!inbox.length) { setHTML("inbox-list", emptyState("community", state.inbox.length ? "Nothing in this corner of campus." : "All quiet on the question front.", state.inbox.length ? "Try another topic or search to find a question you can help with." : "New questions from other students will appear here. Your own questions and ones you’ve answered stay out of your inbox.")); return; }
    setHTML("inbox-list", inbox.map(q => {
      const cat = category(q.category), busy = state.busyAnswers.has(String(q.id)), disabled = busy || q.closes_at <= now();
      const num = Math.max(0, Number(q.n_answers) || 0), quorum = Math.max(1, Number(q.quorum || state.config?.quorum) || 3);
      const endorsements = q.answer_type === "text" && q.answer_options?.length ? `<section class="endorsement-options" aria-label="Existing student answers"><h4>Agree with an answer?</h4><p>Endorse one below, or write your own response.</p>${q.answer_options.map(answer => `<div class="endorsement-option"><p class="endorsement-copy">${esc(answer.value)}</p><div class="endorsement-actions"><span>${answer.count} student${answer.count === 1 ? "" : "s"} support${answer.count === 1 ? "s" : ""} this</span><button class="button button-outline button-small" id="endorse-${esc(q.id)}-${esc(answer.id)}" data-endorse-question="${esc(q.id)}" data-endorse-answer="${esc(answer.id)}" ${disabled ? "disabled" : ""}>${busy ? "Sending…" : "Endorse this answer"} ${icon("check")}</button></div></div>`).join("")}</section>` : "";
      const control = q.answer_type === "yes_no" ? `<div class="answer-buttons">${["Yes","No","Not sure"].map(value => `<button id="answer-${esc(q.id)}-${value.replace(/ /g,"-")}" data-answer-id="${esc(q.id)}" data-answer-value="${value}" ${disabled ? "disabled" : ""}>${busy ? "Sending…" : value}</button>`).join("")}</div>` : `<form class="answer-form" data-answer-form="${esc(q.id)}"><label for="answer-text-${esc(q.id)}">${q.answer_options?.length ? "Or write your own response" : "What do you know?"}</label><textarea id="answer-text-${esc(q.id)}" data-answer-draft="${esc(q.id)}" maxlength="500" rows="2" placeholder="Share a specific, firsthand answer…" required ${disabled ? "disabled" : ""}>${esc(state.answerDrafts.get(String(q.id)) || "")}</textarea><button class="button button-outline button-small" type="submit" ${disabled ? "disabled" : ""}>${busy ? "Sharing your answer…" : "Share your answer"} ${icon("arrow")}</button></form>`;
      return `<article class="inbox-card" id="crowd-${esc(q.id)}"><div class="item-topline"><span class="category-label">${icon(categoryIcons[cat])}${esc(categories[cat])}</span><span class="badge ${q.is_demo ? "badge-amber" : "badge-muted"}"><span class="deadline" data-deadline="${esc(q.closes_at)}">${esc(countdownLabel(q.closes_at))}</span></span></div><h3>${esc(q.prompt)}</h3><div class="crowd-progress"><label for="progress-${esc(q.id)}"><span>${num} of ${quorum} responses needed</span><span>${Math.max(0, quorum - num)} to go</span></label><progress id="progress-${esc(q.id)}" max="${quorum}" value="${Math.min(num,quorum)}"></progress></div>${endorsements}${control}<p class="form-error" id="answer-error-${esc(q.id)}" role="alert" hidden></p></article>`;
    }).join(""));
  }
  function renderContributions() {
    if (!state.user) { setHTML("contributions-list", emptyState("check", "Make a little campus impact.", "Your answers help other students find their way. Sign in to see your contributions.", authCTA())); return; }
    setHTML("contributions-list", state.contributions.length ? state.contributions.map(c => `<article class="contribution-card"><span class="square-icon">${icon("check")}</span><div class="contribution-copy"><h3>${esc(c.prompt)}</h3><p>Your answer: ${esc(c.value)}</p><small title="${esc(dateTitle(c.created_at))}">${esc(relativeTime(c.created_at))}</small></div><span class="badge ${c.status === "open" || c.status === "pending" ? "badge-amber" : "badge-green"}">${c.status === "open" || c.status === "pending" ? "Collecting" : "Closed"}</span></article>`).join("") : emptyState("check", "Your knowledge can make a difference.", "Answer an open community question and your contribution will appear here.", '<button class="button button-outline" data-show-inbox="true">Find a question to answer '+icon("arrow")+'</button>'));
  }
  function renderFacts(view) {
    renderCategories(view);
    const live = view === "live";
    if (!state.loaded) { setHTML(`${view}-list`, '<div class="loading-skeleton" aria-label="Loading campus knowledge"></div><div class="loading-skeleton" aria-hidden="true"></div>'); return; }
    if (live && !state.user) { setHTML("live-list", emptyState("live", "The latest word around campus.", "Sign in to see fresh answers backed by student responses, including when they expire.", authCTA("See what campus knows"))); return; }
    const allFacts = (live ? state.facts.live.filter(f => f.valid_until > now()) : state.facts.static) || [];
    if (!live) $("knowledge-count").textContent = `${allFacts.length} campus facts`;
    const facts = allFacts.filter(f => (state.filters[view] === "all" || category(f.category) === state.filters[view]) && matchText(`${f.title || ""} ${f.entity || ""} ${f.question || ""} ${f.value || ""} ${f.text || ""}`, state.search[view]));
    if (!facts.length) {
      setHTML(`${view}-list`, emptyState(live ? "live" : "book", allFacts.length ? "No matches, but plenty to explore." : live ? "Fresh answers start with a question." : "Campus knowledge is on its way.", allFacts.length ? "Try another topic or a different search." : live ? "When students reach a consensus, the answer appears here until it expires. Ask something about campus to get things started." : "Published campus facts will appear here once they’re available.", live && !allFacts.length ? '<a class="button button-primary" href="#ask">Ask a campus question '+icon("arrow")+'</a>' : ""));
      return;
    }
    setHTML(`${view}-list`, facts.map(f => {
      const cat = category(f.category), url = safeURL(f.source_url);
      const title = live ? f.question || `${f.entity} · ${f.attribute}` : f.title || f.entity || "Campus knowledge";
      return `<article class="fact-card"><div class="item-topline"><span class="category-label">${icon(categoryIcons[cat])}${esc(categories[cat])}</span><span class="badge ${live ? f.is_demo ? "badge-amber" : "badge-green" : "badge-muted"}">${live ? f.is_demo ? "Simulation" : "Community" : "Campus guide"}</span></div><h3>${esc(title)}</h3>${f.report_summary ? reportSummaryHTML(f.report_summary) : `<p class="fact-value">${esc(f.value || f.text)}</p>`}${live ? `<div class="fact-freshness"><span>${icon("community")}${esc(f.n_votes)} responses</span>${f.report_summary ? "" : `<span>${Math.round((f.confidence || 0) * 100)}% agreement</span>`}</div><div class="fact-footer"><span>${esc(expiryLabel(f.valid_until))}</span>${icon("clock")}</div>` : `<div class="fact-footer"><span>Published campus information</span>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">View source ${icon("up-right")}</a>` : '<span>Source unavailable</span>'}</div>`}</article>`;
    }).join(""));
  }
  function renderNotifications() {
    const unread = state.notifications.filter(n => !n.read).length;
    $("notification-dot").hidden = !unread;
    $("notification-button").setAttribute("aria-label", `Open notifications${unread ? `, ${unread} unread` : ""}`);
    $("notification-summary").textContent = unread ? `${unread} unread update${unread === 1 ? "" : "s"}` : "You’re all caught up";
    $("mark-read").disabled = !unread;
    if (!state.user) { setHTML("notifications-list", emptyState("bell", "Your next answer is worth a heads-up.", "Sign in to see when your questions are answered and when campus needs your help.", authCTA("Sign in for updates"))); return; }
    setHTML("notifications-list", state.notifications.length ? state.notifications.map(n => `<button class="notification-item ${n.read ? "" : "unread"}" data-notification-id="${esc(n.id)}"><span class="square-icon">${icon(n.kind === "answer_ready" || n.query_id ? "check" : "ask")}</span><span class="notification-copy"><strong>${esc(n.title)}</strong><p>${esc(n.body)}</p><small title="${esc(dateTitle(n.created_at))}">${esc(relativeTime(n.created_at))}</small></span>${n.read ? "" : '<span class="unread-dot"></span>'}</button>`).join("") : emptyState("bell", "A quiet moment. Enjoy it.", "Your answer updates and community questions will appear here."));
  }
  function renderAll() {
    renderAccount(); renderConfig(); renderStats(); renderQueries(); renderInbox(); renderContributions(); renderFacts("live"); renderFacts("knowledge"); renderNotifications();
  }
  function browserNotify(notifications) {
    for (const note of notifications) {
      if (state.notificationsInitialized && !state.seenNotifications.has(note.id) && !note.read && state.browserAlerts && "Notification" in window && Notification.permission === "granted") {
        try {
          const alert = new Notification(note.title, {body:note.body,icon:"/static/icon-192.png",tag:`gramask-${note.id}`});
          const version = state.requestVersion;
          alert.onclick = () => { window.focus(); if (version === state.requestVersion) openNotification(note); alert.close(); };
        } catch { /* Notifications are optional; the in-app update remains available. */ }
      }
      state.seenNotifications.add(note.id);
    }
    state.notificationsInitialized = true;
  }
  async function refresh() {
    if (state.authBusy) { state.refreshQueued = true; return; }
    if (state.refreshing) { state.refreshQueued = true; return; }
    state.refreshing = true;
    try {
      if (!await syncSession()) { state.refreshQueued = true; return; }
      const version = state.requestVersion;
      const hasUser = !!state.user;
      const requests = [["config","/api/config"],["facts","/api/facts"]];
      if (hasUser) requests.push(["stats","/api/stats"],["queries","/api/queries"],["inbox","/api/inbox"],["contributions","/api/contributions"],["notifications","/api/notifications"]);
      if (hasUser && state.config?.demo_mode && !state.user.is_demo_student) requests.push(["students","/api/demo/students"]);
      const results = await Promise.allSettled(requests.map(([,path]) => api(path, undefined, {timeout:15000})));
      if (version !== state.requestVersion) return;
      // Cookies are shared between tabs. Verify the same session still owns this batch
      // before any private response becomes visible, even without BroadcastChannel.
      if (!await syncSession() || version !== state.requestVersion) { state.refreshQueued = true; return; }
      const errors = [];
      results.forEach((result,index) => {
        const key = requests[index][0];
        if (result.status === "fulfilled") {
          if (key === "notifications") browserNotify(result.value);
          state[key] = result.value;
          if (key === "config" && result.value.server_time) state.clockOffset = result.value.server_time - Date.now() / 1000;
          if (key === "facts") state.loaded = true;
        } else errors.push(result.reason.message);
      });
      $("connection-banner").hidden = !errors.length;
      $("connection-message").textContent = errors.length ? `Some information couldn’t refresh. ${errors[0]}` : "";
      renderAll();
    } catch (error) {
      $("connection-banner").hidden = false;
      $("connection-message").textContent = `Some information couldn’t refresh. ${error.message}`;
    } finally {
      state.refreshing = false;
      if (state.refreshQueued) { state.refreshQueued = false; void refresh(); }
    }
  }
  function setView(view, focus = false) {
    if (!["ask","community","live","knowledge"].includes(view)) view = "ask";
    state.view = view;
    document.querySelectorAll(".page-view").forEach(node => { node.hidden = node.id !== `view-${view}`; });
    document.querySelectorAll("[data-view]").forEach(link => {
      const active = link.dataset.view === view;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    document.title = `${state.user?.is_demo_student ? state.user.display_name + " · " : ""}${{ask:"Ask GramAsk",community:"Community",live:"Live answers",knowledge:"Knowledge base"}[view]} — GramAsk`;
    setMenu(false);
    if (focus) { $("main").focus({preventScroll:true}); window.scrollTo({top:0,behavior:"instant"}); }
  }
  function navigate(view) { if (location.hash === `#${view}`) setView(view, true); else location.hash = view; }
  function setMenu(open) {
    const mobile = window.matchMedia("(max-width:760px)").matches;
    if (!mobile) open = false;
    $("sidebar").classList.toggle("open", open);
    $("sidebar").inert = mobile && !open;
    $("sidebar").setAttribute("aria-hidden", String(mobile && !open));
    $("sidebar-backdrop").hidden = !open;
    $("menu-button").setAttribute("aria-expanded", String(open));
    $("menu-button").setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    document.body.classList.toggle("menu-open",open);
  }
  function setAuthMode(mode) {
    state.authMode = mode;
    const register = mode === "register";
    $("display-name-field").hidden = !register;
    $("display-name").required = register;
    $("password-hint").hidden = !register;
    $("password").minLength = register ? 10 : 1;
    $("password").autocomplete = register ? "new-password" : "current-password";
    $("auth-submit").innerHTML = `${register ? "Create your account" : "Sign in"} ${icon("arrow")}`;
    document.querySelectorAll("[data-auth-mode]").forEach(button => { const active = button.dataset.authMode === mode; button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1; });
    showError("auth-error");
  }
  function openAuth(mode = "login", action = null) {
    if (state.user) { if (action) action(); return; }
    authAction = action;
    setAuthMode(mode);
    $("notification-dialog").close();
    if (!$("auth-dialog").open) $("auth-dialog").showModal();
    setTimeout(() => $(mode === "register" ? "display-name" : "username").focus(), 0);
  }
  function openAccount() { if (state.user) { renderAccount(); $("account-dialog").showModal(); } else openAuth("login"); }
  async function submitAuth(event) {
    event.preventDefault();
    if (state.authBusy) return;
    const mode = state.authMode;
    state.authBusy = true;
    const submit = $("auth-submit");
    submit.disabled = true; submit.textContent = mode === "register" ? "Making yourself at home…" : "Signing in…";
    document.querySelectorAll("[data-auth-mode]").forEach(button => { button.disabled = true; });
    showError("auth-error");
    const sessionVersion = state.requestVersion;
    try {
      const body = {username:$("username").value.trim(),password:$("password").value};
      if (mode === "register") body.display_name = $("display-name").value.trim();
      const result = await api(`/api/auth/${mode}`,body,{auth:true,timeout:20000});
      if (sessionVersion !== state.requestVersion) return;
      const questionDraft = $("ask-input").value;
      clearPrivateState();
      state.user = result.user; state.csrf = result.csrf_token;
      $("ask-input").value = questionDraft;
      state.seenNotifications.clear(); state.notificationsInitialized = false;
      $("auth-dialog").close(); $("auth-form").reset();
      $("password").type = "password"; $("show-password").textContent = "Show";
      const callback = authAction; authAction = null;
      toast(`You’re in, ${state.user.display_name}. Welcome to the conversation.`);
      announceAuthChange();
      state.authBusy = false;
      renderAll(); await refresh();
      if (callback) await callback();
    } catch (error) { if (sessionVersion === state.requestVersion) showError("auth-error",error.message); }
    finally {
      state.authBusy = false; submit.disabled = false;
      submit.innerHTML = `${state.authMode === "register" ? "Create your account" : "Sign in"} ${icon("arrow")}`;
      document.querySelectorAll("[data-auth-mode]").forEach(button => { button.disabled = false; });
      if (state.refreshQueued) void refresh();
    }
  }
  async function submitQuestion(event) {
    event?.preventDefault();
    const text = $("ask-input").value.trim();
    if (!text || state.askBusy) return;
    if (!state.user) { openAuth("register", () => submitQuestion()); return; }
    state.askBusy = true; $("ask-button").disabled = true; $("ask-button").textContent = "Finding your answer…"; $("ask-input").readOnly = true;
    showError("ask-error");
    const sessionVersion = state.requestVersion;
    try {
      // Static questions can need two model calls, each capped at 300 seconds.
      const query = await api("/api/ask",{text},{timeout:620000});
      if (sessionVersion !== state.requestVersion) return;
      state.queries = [query,...state.queries.filter(q => q.id !== query.id)];
      $("ask-input").value = "";
      state.filters.queries = "all"; state.search.queries = ""; $("question-search").value = "";
      updateQueryFilter(); renderQueries();
      toast(query.status === "pending" ? "Your question is out there. We’re checking with the community." : "Your answer is ready.");
      highlightQuery(query.id);
      void refresh();
    } catch (error) { if (sessionVersion === state.requestVersion) showError("ask-error",error.message); }
    finally { if (sessionVersion === state.requestVersion) { state.askBusy = false; $("ask-button").disabled = false; $("ask-button").innerHTML = `Ask GramAsk ${icon("arrow")}`; $("ask-input").readOnly = false; } }
  }
  async function sendAnswer(id, value = "", endorseAnswerId = null) {
    if ((!endorseAnswerId && !value.trim()) || state.busyAnswers.has(String(id))) return;
    if (!state.user) { openAuth(); return; }
    state.busyAnswers.add(String(id)); renderInbox();
    const sessionVersion = state.requestVersion;
    try {
      await api("/api/answer",{question_id:id,...(endorseAnswerId ? {endorse_answer_id:Number(endorseAnswerId)} : {value:value.trim()})},{timeout:320000});
      if (sessionVersion !== state.requestVersion) return;
      state.inbox = state.inbox.filter(q => String(q.id) !== String(id)); state.answerDrafts.delete(String(id));
      renderInbox(); renderStats(); toast(endorseAnswerId ? "Endorsed. Your response has been counted." : "Thanks. Your response has been counted.");
      void refresh();
    } catch (error) {
      if (sessionVersion !== state.requestVersion) return;
      state.busyAnswers.delete(String(id)); renderInbox();
      const errorNode = $(`answer-error-${id}`);
      if (errorNode) { errorNode.textContent = error.message; errorNode.hidden = false; } else toast(error.message,true);
    } finally { if (sessionVersion === state.requestVersion) state.busyAnswers.delete(String(id)); }
  }
  function updateQueryFilter() { document.querySelectorAll("[data-query-filter]").forEach(button => { const active = button.dataset.queryFilter === state.filters.queries; button.classList.toggle("selected",active); button.setAttribute("aria-pressed", String(active)); }); }
  function setCommunityTab(tab) {
    state.communityTab = tab;
    $("inbox-panel").hidden = tab !== "inbox";
    $("contributions-panel").hidden = tab !== "contributions";
    document.querySelectorAll("[data-community-tab]").forEach(button => { const active = button.dataset.communityTab === tab; button.setAttribute("aria-selected",String(active)); button.tabIndex = active ? 0 : -1; });
  }
  function highlightQuery(id) {
    requestAnimationFrame(() => {
      const element = $(`query-${id}`);
      if (!element) return;
      element.scrollIntoView({behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",block:"nearest"});
      element.classList.add("question-highlight");
      setTimeout(() => element.classList.remove("question-highlight"),2700);
    });
  }
  async function markRead() {
    if (!state.user || !state.notifications.some(n => !n.read)) return;
    $("mark-read").disabled = true;
    const sessionVersion = state.requestVersion;
    try { await api("/api/notifications/read",{}); if (sessionVersion !== state.requestVersion) return; state.notifications.forEach(n => { n.read = true; }); renderNotifications(); }
    catch(error) { if (sessionVersion === state.requestVersion) { toast(error.message,true); $("mark-read").disabled = false; } }
  }
  function openNotification(note) {
    $("notification-dialog").close();
    if (note.query_id) {
      state.filters.queries = "all"; state.search.queries = ""; $("question-search").value = "";
      updateQueryFilter(); renderQueries(); navigate("ask"); setTimeout(() => highlightQuery(note.query_id),50);
    } else if (note.crowd_question_id) {
      setCommunityTab("inbox"); navigate("community");
      setTimeout(() => $(`crowd-${note.crowd_question_id}`)?.scrollIntoView({block:"center",behavior:"smooth"}),50);
    }
  }
  function renderBrowserPermission() {
    const supported = "Notification" in window && window.isSecureContext;
    const button = $("enable-notifications");
    if (!supported) { button.textContent = "Browser alerts aren’t supported here"; button.disabled = true; return; }
    if (Notification.permission === "denied") { button.textContent = "Alerts blocked · change browser permissions to enable"; button.disabled = true; return; }
    button.disabled = false;
    button.textContent = state.browserAlerts && Notification.permission === "granted" ? "Turn off browser alerts" : "Enable browser alerts";
  }
  async function toggleNotifications() {
    if (!("Notification" in window)) return;
    if (state.browserAlerts) state.browserAlerts = false;
    else {
      const permission = await Notification.requestPermission();
      state.browserAlerts = permission === "granted";
      toast(permission === "granted" ? "Browser alerts are on while GramAsk is open." : "Your updates will still appear here in GramAsk.");
    }
    try { localStorage.setItem("gramask_browser_alerts",String(state.browserAlerts)); } catch { /* optional preference */ }
    renderBrowserPermission();
  }
  async function demoAction(action) {
    if (state.demoBusy || !state.user || !state.config?.demo_mode) return;
    state.demoBusy = true;
    $("demo-seed").disabled = true; $("demo-answer").disabled = true; $("demo-reset").disabled = true;
    $("demo-status").textContent = action === "seed" ? "Creating your sample question…" : action === "answer" ? "Simulating student responses…" : "Clearing your demo activity…";
    const sessionVersion = state.requestVersion;
    try {
      if (action === "seed") {
        const query = await api("/api/demo/seed-question",{});
        if (sessionVersion !== state.requestVersion) return;
        state.demoQuestion = query.crowd_question_id;
        state.queries = [query,...state.queries.filter(q => q.id !== query.id)]; renderQueries();
        $("demo-status").textContent = "Question created. Answer it from the three student tabs above, or use the automatic simulation.";
      } else if (action === "answer") {
        await api(`/api/demo/auto-answer?question_id=${encodeURIComponent(state.demoQuestion)}`,{});
        if (sessionVersion !== state.requestVersion) return;
        $("demo-status").textContent = "Simulated responses submitted. Your answer and a temporary demo fact will appear below.";
      } else {
        await api("/api/demo/reset",{});
        if (sessionVersion !== state.requestVersion) return;
        state.demoQuestion = null;
        $("demo-status").textContent = "Your demo activity has been cleared. You can run it again.";
      }
      await refresh();
    } catch(error) { if (sessionVersion === state.requestVersion) { $("demo-status").textContent = error.message; toast(error.message,true); } }
    finally { if (sessionVersion === state.requestVersion) { state.demoBusy = false; $("demo-seed").disabled = false; $("demo-reset").disabled = false; renderConfig(); } }
  }
  async function prepareStudents() {
    if (state.studentsBusy || !state.user || !state.config?.demo_mode) return;
    const version = state.requestVersion;
    state.studentsBusy = true; renderConfig();
    $("student-status").textContent = "Preparing three separate student accounts…";
    try {
      const accounts = await api("/api/demo/students", {});
      if (version !== state.requestVersion) return;
      state.students = accounts;
      $("student-status").textContent = "Ready. Open each student link in its own tab. Your account here stays signed in.";
    } catch (error) {
      if (version === state.requestVersion) $("student-status").textContent = error.message;
    } finally {
      if (version === state.requestVersion) { state.studentsBusy = false; renderConfig(); }
    }
  }
  function setupEvents() {
    $("prepare-students").addEventListener("click", prepareStudents);
    $("student-links").addEventListener("click", event => {
      if (!event.target.closest("[data-student-inbox]")) return;
      event.preventDefault();
      state.filters.community = "all"; state.search.community = "";
      $("community-search").value = "";
      setCommunityTab("inbox"); renderInbox(); navigate("community");
      void refresh();
    });
    $("ask-form").addEventListener("submit",submitQuestion);
    $("ask-input").addEventListener("keydown",event => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); $("ask-form").requestSubmit(); } });
    $("auth-form").addEventListener("submit",submitAuth);
    $("account-button").addEventListener("click",openAccount);
    $("topbar-account").addEventListener("click",openAccount);
    $("menu-button").addEventListener("click",() => { const open = !$("sidebar").classList.contains("open"); setMenu(open); if (open) $("sidebar").querySelector(".nav-link").focus(); });
    $("sidebar-backdrop").addEventListener("click",() => { setMenu(false); $("menu-button").focus(); });
    document.addEventListener("keydown",event => {
      if (!$("sidebar").classList.contains("open")) return;
      if (event.key === "Escape") { setMenu(false); $("menu-button").focus(); }
      if (event.key === "Tab") {
        const focusable = [...$("sidebar").querySelectorAll("a,button:not(:disabled)")];
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    window.addEventListener("resize",() => setMenu($("sidebar").classList.contains("open")));
    window.addEventListener("hashchange",() => setView(location.hash.slice(1),true));
    $("auth-close").addEventListener("click",() => { $("auth-dialog").close(); authAction = null; });
    $("auth-dialog").addEventListener("cancel",() => { authAction = null; });
    $("account-close").addEventListener("click",() => $("account-dialog").close());
    $("notification-close").addEventListener("click",() => $("notification-dialog").close());
    for (const dialog of document.querySelectorAll("dialog")) {
      dialog.addEventListener("click",event => { if (event.target !== dialog) return; const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) { dialog.close(); if (dialog.id === "auth-dialog") authAction = null; } });
    }
    $("show-password").addEventListener("click",() => { const show = $("password").type === "password"; $("password").type = show ? "text" : "password"; $("show-password").textContent = show ? "Hide" : "Show"; $("show-password").setAttribute("aria-label",show ? "Hide password" : "Show password"); });
    $("notification-button").addEventListener("click",() => { renderBrowserPermission(); renderNotifications(); $("notification-dialog").showModal(); });
    $("mark-read").addEventListener("click",markRead);
    $("enable-notifications").addEventListener("click",toggleNotifications);
    $("retry-button").addEventListener("click",async () => { $("retry-button").disabled = true; await refresh(); $("retry-button").disabled = false; });
    $("signout-button").addEventListener("click",async () => {
      $("signout-button").disabled = true;
      const version = state.requestVersion;
      try { await api("/api/auth/logout",{}); if (version !== state.requestVersion) return; clearPrivateState(); $("account-dialog").close(); $("ask-input").value = ""; showError("ask-error"); renderAll(); announceAuthChange(); toast("You’re signed out. See you around the yard."); void refresh(); }
      catch(error) { if (version === state.requestVersion) toast(error.message,true); }
      finally { $("signout-button").disabled = false; }
    });
    $("global-search-form").addEventListener("submit",event => { event.preventDefault(); state.search.knowledge = $("global-search").value.trim(); $("knowledge-search").value = state.search.knowledge; state.filters.knowledge = "all"; renderFacts("knowledge"); navigate("knowledge"); });
    for (const [id,key] of [["question-search","queries"],["community-search","community"],["live-search","live"],["knowledge-search","knowledge"]]) {
      $(id).addEventListener("input",event => { state.search[key] = event.target.value; if (key === "queries") renderQueries(); else if (key === "community") renderInbox(); else renderFacts(key); });
    }
    $("demo-seed").addEventListener("click",() => demoAction("seed"));
    $("demo-answer").addEventListener("click",() => demoAction("answer"));
    $("demo-reset").addEventListener("click",() => demoAction("reset"));
    document.addEventListener("input",event => { const draftId = event.target.dataset.answerDraft; if (draftId) state.answerDrafts.set(draftId,event.target.value); });
    document.addEventListener("submit",event => { const id = event.target.dataset.answerForm; if (id) { event.preventDefault(); void sendAnswer(id,$(`answer-text-${id}`).value); } });
    document.addEventListener("click",event => {
      const target = event.target.closest("button");
      if (!target || target.disabled) return;
      const data = target.dataset;
      if (data.prompt) { navigate("ask"); $("ask-input").value = data.prompt; $("ask-input").focus(); }
      if (data.openAuth) openAuth(data.openAuth);
      if (data.authMode) setAuthMode(data.authMode);
      if (data.queryFilter) { state.filters.queries = data.queryFilter; updateQueryFilter(); renderQueries(); }
      if (data.category) { state.filters[data.categoryView] = data.category; if (data.categoryView === "community") renderInbox(); else renderFacts(data.categoryView); }
      if (data.answerId) void sendAnswer(data.answerId,data.answerValue);
      if (data.endorseQuestion) void sendAnswer(data.endorseQuestion,"",data.endorseAnswer);
      if (data.communityTab) setCommunityTab(data.communityTab);
      if (data.showInbox) setCommunityTab("inbox");
      if (data.reask) { const query = state.queries.find(q => String(q.id) === data.reask); if (query) { $("ask-input").value = query.text; $("ask-input").focus(); $("ask-input").scrollIntoView({block:"center",behavior:"smooth"}); } }
      if (data.notificationId) { const note = state.notifications.find(n => String(n.id) === data.notificationId); if (note) openNotification(note); }
    });
    document.querySelectorAll('[role="tablist"]').forEach(list => {
      list.addEventListener("keydown",event => {
        if (!["ArrowRight","ArrowLeft","Home","End"].includes(event.key)) return;
        const tabs = [...list.querySelectorAll('[role="tab"]')];
        const index = tabs.indexOf(document.activeElement); if (index < 0) return;
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        tabs[next].click(); tabs[next].focus();
      });
    });
    document.addEventListener("visibilitychange",() => { if (!document.hidden) { void refresh(); } });
    window.addEventListener("online",() => void refresh());
    const receiveAuthChange = () => { clearPrivateState(); renderAll(); void refresh(); };
    authChannel?.addEventListener("message",event => { if (event.data?.type === "auth-changed") receiveAuthChange(); });
    window.addEventListener("storage",event => { if (!authChannel && event.key === authStorageKey) receiveAuthChange(); });
    window.addEventListener("offline",() => { $("connection-banner").hidden = false; $("connection-message").textContent = "You’re offline. Reconnect to ask questions and see new campus answers."; });
    // The browser must remain open for these alerts. No background push subscription is claimed.
    const poll = () => {
      if (!document.hidden) { hiddenPollTicks = 0; void refresh(); }
      else if (state.browserAlerts && ++hiddenPollTicks >= 3) { hiddenPollTicks = 0; void refresh(); }
    };
    pollTimer = setInterval(poll,5000);
    countdownTimer = setInterval(updateCountdowns,1000);
    window.addEventListener("pagehide",() => { clearInterval(pollTimer); clearInterval(countdownTimer); });
    window.addEventListener("pageshow",event => { if (event.persisted) { pollTimer = setInterval(poll,5000); countdownTimer = setInterval(updateCountdowns,1000); void refresh(); } });
  }
  async function start() {
    setupEvents();
    setView(location.hash.slice(1));
    try { state.browserAlerts = localStorage.getItem("gramask_browser_alerts") === "true"; } catch { /* preference is optional */ }
    const version = state.requestVersion;
    try { const auth = await api("/api/auth/me",undefined,{auth:true,timeout:15000}); if (version === state.requestVersion) { state.user = auth.user; state.csrf = auth.csrf_token; } }
    catch(error) { if (version === state.requestVersion && error.status !== 401) { $("connection-banner").hidden = false; $("connection-message").textContent = error.message; } }
    state.authChecked = true;
    renderAll(); await refresh();
    if ("serviceWorker" in navigator && window.isSecureContext) navigator.serviceWorker.register("/sw.js").catch(() => { /* The app still works without offline shell support. */ });
  }
  void start();
})();
