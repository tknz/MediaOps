(function () {
  const tabbar = document.querySelector('.settings-tabbar');
  if (!tabbar) return;

  const tabs = Array.from(tabbar.querySelectorAll('[data-settings-tab]'));
  const panels = Array.from(document.querySelectorAll('[data-settings-panel]'));

  function activate(name, updateHash) {
    tabs.forEach((tab) => {
      const active = tab.dataset.settingsTab === name;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    panels.forEach((panel) => {
      panel.classList.toggle('active', panel.dataset.settingsPanel === name);
    });
    if (updateHash) history.replaceState(null, '', `#${name}`);
    const returnTo = document.querySelector('input[name="return_to"][data-return-to-base]');
    if (returnTo) returnTo.value = `${returnTo.dataset.returnToBase || location.pathname}#${name}`;
  }

  tabs.forEach((tab) => {
    tab.setAttribute('role', 'tab');
    tab.addEventListener('click', () => activate(tab.dataset.settingsTab, true));
  });

  panels.forEach((panel) => panel.setAttribute('role', 'tabpanel'));
  document.querySelectorAll('form.settings').forEach((form) => {
    form.addEventListener('submit', () => {
      const active = document.querySelector('[data-settings-tab].active')?.dataset.settingsTab || 'plex';
      const returnTo = form.querySelector('input[name="return_to"][data-return-to-base]');
      if (returnTo) returnTo.value = `${returnTo.dataset.returnToBase || location.pathname}#${active}`;
    });
  });
  document.querySelectorAll('.secret-toggle').forEach((button) => {
    button.addEventListener('click', () => {
      const field = button.closest('.secret-field');
      const input = field && field.querySelector('.secret-input');
      if (!input) return;
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      button.classList.toggle('active', !showing);
      button.setAttribute('aria-label', showing ? 'Show hidden value' : 'Hide hidden value');
      button.setAttribute('title', showing ? 'Show hidden value' : 'Hide hidden value');
    });
  });
  const initial = location.hash.replace('#', '');
  activate(tabs.some((tab) => tab.dataset.settingsTab === initial) ? initial : tabs[0].dataset.settingsTab, false);
})();
