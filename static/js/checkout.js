(() => {
  const form = document.getElementById('checkoutForm');
  if (!form) return;
  const select = document.getElementById('ticket_type');
  const quantity = document.getElementById('quantity');
  function update() {
    const option = select.selectedOptions[0];
    const available = option && !option.disabled;
    const limit = available ? Math.min(Number(form.dataset.maxOrder), Number(option.dataset.stock)) : 0;
    quantity.max = Math.max(1, limit);
    const count = Math.min(Math.max(1, Math.floor(Number(quantity.value) || 1)), Math.max(1, limit));
    quantity.value = count;
    document.getElementById('orderQuantity').textContent = available ? count : 0;
    document.getElementById('quantityHint').textContent = available ? `Up to ${limit} ticket(s) for this selection.` : 'No tickets available online.';
    document.getElementById('total_display').textContent = '$' + (available ? Number(option.dataset.price) * count : 0).toFixed(2);
  }
  if (!select.selectedOptions[0] || select.selectedOptions[0].disabled) {
    const first = [...select.options].find(option => !option.disabled);
    if (first) select.value = first.value;
  }
  select.addEventListener('change', update);
  quantity.addEventListener('input', update);
  update();
})();
