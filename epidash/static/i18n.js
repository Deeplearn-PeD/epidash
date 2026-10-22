/* EpiDash i18n — tiny dictionary; language = ?lang= → localStorage → browser.
 * Loaded before app.js. */
const I18N = {
  en: {
    home_title: "Published dashboards",
    search_placeholder: "Search dashboards…",
    no_match: "No dashboards match your search.",
    no_dashboards: "No published dashboards yet.",
    by: "by",
    back: "← All dashboards",
    export: "⬇ Export",
    export_title: "Export this dashboard",
    export_png: "PNG image",
    export_pdf: "PDF (print)",
    export_json: "JSON data",
    permalink: "🔗 Permalink",
    copied: "✓ Copied!",
    like: "Like this dashboard",
    unlike: "Unlike this dashboard",
    share_title: "Copy a shareable link to this dashboard",
    loading: "Loading…",
    loading_chart: "Loading chart…",
    not_found: "Dashboard not found or not published.",
    load_failed: "Failed to load dashboards: ",
    render_failed: "Failed to render chart: ",
    live: "live",
    ask_btn: "Ask",
    change_btn: "Change",
    ask_title: "Ask EpidBot a question about this dashboard",
    change_title: "Ask EpidBot to change this dashboard",
    input_placeholder: "Ask a question or request a change…",
    working: "EpidBot is working on it…",
    done: "Done.",
    request_failed: "The request failed.",
    timeout: "Timed out waiting for EpidBot.",
    who_user: "You",
    who_agent: "EpidBot",
    who_error: "Error",
    updated: "Dashboard updated.",
    sign_in: "Sign in",
    sign_out: "Sign out",
    login_required: "This dashboard requires login to view.",
    login_cta: "Sign in to view",
  },
  "pt-BR": {
    home_title: "Painéis publicados",
    search_placeholder: "Buscar painéis…",
    no_match: "Nenhum painel corresponde à busca.",
    no_dashboards: "Nenhum painel publicado ainda.",
    by: "por",
    back: "← Todos os painéis",
    export: "⬇ Exportar",
    export_title: "Exportar este painel",
    export_png: "Imagem PNG",
    export_pdf: "PDF (imprimir)",
    export_json: "Dados JSON",
    permalink: "🔗 Link permanente",
    copied: "✓ Copiado!",
    like: "Curtir este painel",
    unlike: "Remover curtida",
    share_title: "Copiar link para compartilhar este painel",
    loading: "Carregando…",
    loading_chart: "Carregando gráfico…",
    not_found: "Painel não encontrado ou não publicado.",
    load_failed: "Falha ao carregar painéis: ",
    render_failed: "Falha ao renderizar gráfico: ",
    live: "ao vivo",
    ask_btn: "Perguntar",
    change_btn: "Alterar",
    ask_title: "Pergunte ao EpidBot sobre este painel",
    change_title: "Peça ao EpidBot para alterar este painel",
    input_placeholder: "Faça uma pergunta ou solicite uma alteração…",
    working: "EpidBot está trabalhando nisso…",
    done: "Pronto.",
    request_failed: "A solicitação falhou.",
    timeout: "Tempo esgotado aguardando o EpidBot.",
    who_user: "Você",
    who_agent: "EpidBot",
    who_error: "Erro",
    updated: "Painel atualizado.",
    sign_in: "Entrar",
    sign_out: "Sair",
    login_required: "Este painel requer login para ser visualizado.",
    login_cta: "Entrar para visualizar",
  },
};

const CURRENT_LANG = (() => {
  const fromQuery = new URLSearchParams(location.search).get("lang");
  if (fromQuery) localStorage.setItem("epidash-lang", fromQuery);
  const stored = localStorage.getItem("epidash-lang");
  const lang = fromQuery || stored || navigator.language || "en";
  return lang.toLowerCase().startsWith("pt") ? "pt-BR" : "en";
})();

document.documentElement.lang = CURRENT_LANG;

function t(key) {
  return (I18N[CURRENT_LANG] && I18N[CURRENT_LANG][key])
    ?? I18N.en[key] ?? key;
}

(function applyStaticI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    const text = t(el.dataset.i18nTitle);
    el.title = text;
    if (el.hasAttribute("aria-label")) el.setAttribute("aria-label", text);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
})();
