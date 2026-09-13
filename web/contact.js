(() => {
  const setLink = (id, url, ready, pending) => {
    const link = document.getElementById(id);
    if (!link) return;
    if (url) {
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = ready;
      link.classList.remove('pending');
      link.removeAttribute('aria-disabled');
    } else link.textContent = pending;
  };
  fetch('/api/public/community', {cache: 'no-store'}).then(response => response.ok ? response.json() : null).then(data => {
    setLink('x-link', data?.x_url, 'Follow the lab on X ↗', 'X link pending');
    setLink('telegram-link', data?.telegram_url, 'Join the Telegram conversation ↗', 'Telegram link pending');
    setLink('email-link', data?.lab_email ? 'mailto:' + data.lab_email : '', 'Email the lab ↗', 'Lab email pending');
  }).catch(() => {});
})();
