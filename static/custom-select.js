/* Custom select — replaces native <select class="input"> dropdowns globally.
 * Builds a themed trigger + popup from each select's <option> children.
 * The original <select> stays in the DOM (hidden) so forms still submit. */
(function () {
  var CHEVRON_SVG = '<svg class="cs-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';
  var openWrap = null;

  function closeAll() {
    if (openWrap) { openWrap.classList.remove('open'); openWrap = null; }
  }

  document.addEventListener('click', function (e) {
    if (openWrap && !openWrap.contains(e.target)) closeAll();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeAll();
  });

  function upgrade(select) {
    if (select._csUpgraded) return;
    select._csUpgraded = true;
    select.style.display = 'none';

    var wrap = document.createElement('div');
    wrap.className = 'cs-wrap';

    var trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'cs-trigger';

    var label = document.createElement('span');
    label.className = 'cs-label';
    trigger.appendChild(label);
    trigger.insertAdjacentHTML('beforeend', CHEVRON_SVG);

    var popup = document.createElement('div');
    popup.className = 'cs-popup';

    wrap.appendChild(trigger);
    wrap.appendChild(popup);
    select.parentNode.insertBefore(wrap, select.nextSibling);

    function syncLabel() {
      var opt = select.options[select.selectedIndex];
      label.textContent = opt ? opt.textContent : '';
    }

    function buildOptions() {
      popup.innerHTML = '';
      var opts = select.options;
      for (var i = 0; i < opts.length; i++) {
        var opt = opts[i];
        var div = document.createElement('div');
        div.className = 'cs-option';
        if (opt.selected) div.classList.add('selected');
        div.textContent = opt.textContent;
        div.dataset.value = opt.value;
        div.dataset.index = i;
        popup.appendChild(div);
      }
    }

    trigger.addEventListener('click', function (e) {
      e.stopPropagation();
      if (wrap.classList.contains('open')) {
        closeAll();
      } else {
        closeAll();
        buildOptions();
        wrap.classList.add('open');
        openWrap = wrap;
        /* scroll selected into view */
        var sel = popup.querySelector('.cs-option.selected');
        if (sel) sel.scrollIntoView({ block: 'nearest' });
      }
    });

    popup.addEventListener('click', function (e) {
      var target = e.target.closest('.cs-option');
      if (!target) return;
      var idx = parseInt(target.dataset.index, 10);
      select.selectedIndex = idx;
      syncLabel();
      closeAll();
      /* fire change event so dependent JS (like movement resolver) reacts */
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });

    /* Keyboard nav when open */
    trigger.addEventListener('keydown', function (e) {
      if (!wrap.classList.contains('open')) {
        if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          trigger.click();
        }
        return;
      }
      var items = Array.from(popup.querySelectorAll('.cs-option'));
      var cur = popup.querySelector('.cs-option.highlighted');
      var idx = cur ? items.indexOf(cur) : -1;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (cur) cur.classList.remove('highlighted');
        idx = Math.min(idx + 1, items.length - 1);
        items[idx].classList.add('highlighted');
        items[idx].scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (cur) cur.classList.remove('highlighted');
        idx = Math.max(idx - 1, 0);
        items[idx].classList.add('highlighted');
        items[idx].scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (idx >= 0) items[idx].click();
      } else if (e.key === 'Escape') {
        closeAll();
      }
    });

    syncLabel();

    /* Watch for external changes (e.g., JS setting selectedIndex) */
    var observer = new MutationObserver(syncLabel);
    observer.observe(select, { childList: true, subtree: true, attributes: true });
    select.addEventListener('change', syncLabel);
  }

  function upgradeAll() {
    var selects = document.querySelectorAll('select.input, select.cs-upgrade');
    for (var i = 0; i < selects.length; i++) upgrade(selects[i]);
  }

  /* Run on load and expose for dynamic content */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', upgradeAll);
  } else {
    upgradeAll();
  }
  window.CSUpgrade = upgradeAll;
})();
