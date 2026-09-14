(() => {
  const dialog = document.querySelector('.download-dialog');
  if (!dialog) return;
  const title = dialog.querySelector('#download-dialog-title');
  const confirm = dialog.querySelector('.download-confirm');
  const status = dialog.querySelector('.download-dialog-status');
  const downloads = {
    history: {title: 'Download execution history', href: '/api/public/history.jsonl.gz', filename: 'pumpwizard-history-pumpfun.jsonl.gz'},
    research: {title: 'Download research dataset', href: '/api/public/research.jsonl.gz', filename: 'pumpwizard-research-pumpfun.jsonl.gz'},
    summary: {title: 'Download current lab snapshot', href: '/api/public/summary', filename: 'pumpwizard-lab-snapshot-pumpfun.json'}
  };
  let selected = null;
  const label = text => { confirm.innerHTML = text; };
  document.querySelectorAll('.download-trigger').forEach(button => button.addEventListener('click', () => {
    selected = downloads[button.dataset.download];
    if (!selected) return;
    title.textContent = selected.title;
    status.replaceChildren();
    confirm.disabled = false;
    label('Download Pump.fun data <span aria-hidden="true">↓</span>');
    dialog.showModal();
  }));
  confirm.addEventListener('click', async () => {
    if (!selected || confirm.disabled) return;
    confirm.disabled = true;
    label('Preparing download…');
    status.textContent = selected === downloads.research ? 'Preparing the research archive. Large archives can take a moment.' : 'Preparing your download…';
    try {
      const response = await fetch(selected.href, {cache: 'no-store'});
      if (!response.ok) {
        const message = await response.json().then(body => body.error).catch(() => 'Download could not be prepared.');
        throw Error(message);
      }
      const blob = await response.blob();
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = selected.filename;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
      status.textContent = 'Download started.';
      label('Download started <span aria-hidden="true">✓</span>');
      dialog.close('complete');
    } catch (error) {
      const fallback = document.createElement('a');
      fallback.href = selected.href;
      fallback.download = selected.filename;
      fallback.textContent = 'Download archive directly';
      status.replaceChildren(document.createTextNode((error.message || 'The prepared download was interrupted.') + ' '), fallback);
      confirm.disabled = false;
      label('Try again <span aria-hidden="true">↻</span>');
    }
  });
})();
