/* Progressive enhancement: native forms/anchors remain the fallback. */
(() => {
  const root = document.querySelector('[data-job-workspace]');
  if (!root) return;
  const aliases = { 'customer-needs': 'parts', 'machine-workspace': 'parts', 'research-results': 'parts', 'parts-ready': 'quote', 'fulfillment-checklist': 'orders', 'operational-activity': 'activity', 'job-overview': 'overview' };
  function reveal() {
    const id = decodeURIComponent(location.hash.slice(1));
    const target = document.getElementById(aliases[id] || id);
    if (!target) return;
    for (let node = target; node && node !== root; node = node.parentElement) {
      if (node instanceof HTMLDetailsElement) node.open = true;
    }
    target.scrollIntoView({ block: 'start' });
    root.querySelectorAll('.jcc-nav a').forEach(link => {
      const section = document.getElementById(link.hash.slice(1));
      if (section === target || section?.contains(target)) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
  }
  window.addEventListener('hashchange', reveal);
  root.addEventListener('click', event => {
    if (event.target.closest('[data-open-details]')) setTimeout(reveal, 0);
  });
  reveal();
  function showError(form, text) {
    let error = form.querySelector('.jcc-error');
    if (!error) {
      error = document.createElement('p');
      error.className = 'jcc-error';
      error.setAttribute('role', 'alert');
      error.tabIndex = -1;
      form.prepend(error);
    }
    error.textContent = text;
    error.focus();
  }
  root.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('[data-jcc-form]')) return;
    event.preventDefault();
    if (root.dataset.saving === '1') return;
    const data = new FormData(form);
    if (event.submitter?.name) data.append(event.submitter.name, event.submitter.value);
    if (form.id === 'quote-builder-form' && !data.getAll('basket_item_ids').length) {
      showError(form, 'Select at least one item for the quote.');
      return;
    }
    root.dataset.saving = '1';
    const button = event.submitter;
    if (button) button.disabled = true;
    form.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(form.action, {method: 'POST', body: data, headers: {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}});
      if (!response.ok) {
        let message = 'Could not save. Your entries are still here. Review them and try again.';
        try {
          const result = await response.json();
          if (typeof result.detail === 'string') message = result.detail;
          else if (Array.isArray(result.detail)) message = result.detail.map(error => `${error.loc?.at(-1) || 'Field'}: ${error.msg}`).join('; ');
        } catch (_) {}
        showError(form, message);
        return;
      }
      const destination = new URL(response.url);
      if (destination.pathname === location.pathname) {
        destination.search = location.search;
        destination.hash = form.dataset.return || location.hash || 'parts';
        // A hash-only location.assign would keep stale cards and revision tokens.
        history.pushState(null, '', destination.href);
        location.reload();
        return;
      }
      // A fresh GET carries fresh revision tokens; never retry a mutation automatically.
      location.assign(destination.href);
    } catch (_) {
      showError(form, 'Connection interrupted. Your entries are still here. Check the job before retrying; the save may have completed.');
    } finally {
      root.dataset.saving = '0';
      if (button) button.disabled = false;
      form.removeAttribute('aria-busy');
    }
  });
  const review = root.querySelector('#quote-builder-form');
  if (review) {
    const output = review.querySelector('[data-quote-total]');
    const refresh = () => {
      const lines = [...review.querySelectorAll('[data-line-total]:checked')];
      output.value = (Number(output.dataset.charges) + lines.reduce((total, line) => total + Number(line.dataset.lineTotal), 0)).toFixed(2);
    };
    review.addEventListener('change', refresh);
    refresh();
  }
  const quickInput = root.querySelector('#quick-add-input');
  const quickButton = root.querySelector('#quick-add-preview-button');
  const quickPreview = root.querySelector('#quick-add-preview');
  if (quickInput && quickButton && quickPreview) {
    const parse = (text) => text.split(/\n\s*\n/).map(block => {
      const lines = block.split('\n').map(line => line.trim()).filter(Boolean);
      const get = (re) => (lines.find(line => re.test(line)) || '').replace(/^[^:]+:\s*/i, '').trim();
      const description = lines.find(line => !/^(pn|part|cost|sell|price|qty|quantity|supplier|oem|in stock|availability|stock)/i.test(line)) || '';
      return {description, supplier:get(/^supplier/i) || lines.find(line => /parts|hydraulics|dealer|supply/i.test(line)) || '', part:get(/^(pn|part\s*#|oem)/i), cost:get(/^cost/i), sell:get(/^(sell|price)/i), qty:get(/^(qty|quantity)/i) || '1', availability: get(/(in stock|availability|stock)/i) || (lines.find(line => /in stock|backorder|ships/i.test(line)) || '')};
    }).filter(item => item.description);
    quickButton.addEventListener('click', () => {
      const items = parse(quickInput.value);
      quickPreview.innerHTML = '';
      if (!items.length) { quickPreview.textContent = 'Paste one or more part blocks to preview.'; quickPreview.hidden = false; return; }
      items.forEach(item => {
        const card = document.createElement('article'); card.className = 'jcc-quick-card';
        card.innerHTML = `<h3>${item.description}</h3><label>Supplier<input value="${item.supplier}" data-field="supplier_name"></label><label>Part #<input value="${item.part}" data-field="supplier_part_number"></label><label>Cost<input type="number" inputmode="decimal" value="${item.cost}" data-field="supplier_unit_cost"></label><label>Sell<input type="number" inputmode="decimal" value="${item.sell}" data-field="customer_unit_price_override"></label><label>Qty<input type="number" inputmode="numeric" min="1" value="${item.qty}" data-field="quantity"></label><label>Availability<input value="${item.availability}" data-field="availability"></label><button type="button" class="button" data-quick-add>Add to Job</button>`;
        card.querySelector('[data-quick-add]').addEventListener('click', async () => {
          const form = new FormData(); form.append('csrf_token', root.querySelector('[name=csrf_token]')?.value || ''); form.append('description', item.description); form.append('job_asset_id', '');
          const match = [...root.querySelectorAll('[data-requested-need-id]')].find(node => node.querySelector('h3')?.textContent.toLowerCase().includes(item.description.toLowerCase()) || item.description.toLowerCase().includes(node.querySelector('h3')?.textContent.toLowerCase() || '___'));
          form.append('requested_need_id', match?.dataset.requestedNeedId || '');
          card.querySelectorAll('[data-field]').forEach(input => form.append(input.dataset.field, input.value));
          form.append('manufacturer_part_number', ''); form.append('source_type', 'AFTERMARKET'); form.append('verification_status', 'NEEDS_REVIEW');
          const response = await fetch(`/jobs/${root.dataset.jobId}/research-results/manual`, {method:'POST', body:form, headers:{'X-Requested-With':'XMLHttpRequest'}});
          if (response.ok) { card.innerHTML = '<strong>Added to this job</strong>'; } else { card.insertAdjacentHTML('beforeend','<p class="jcc-error">Could not add this item. Review the fields and try again.</p>'); }
        });
        quickPreview.append(card);
      });
      quickPreview.hidden = false;
    });
  }
})();
