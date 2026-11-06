const CONTACT_EMAIL = 'max@eiye.ai';

function commercialMailto() {
  const subject = encodeURIComponent('eiye commercial inquiry');
  const body = encodeURIComponent(
    [
      'I would like to discuss a commercial eiye license.',
      '',
      'Organization:',
      'Approx. datasources / query volume:',
      'Interest (SSO / compliance reports / multi-tenant control plane / advanced analytics / Enterprise):',
      'Notes:',
    ].join('\n')
  );
  return 'mailto:' + CONTACT_EMAIL + '?subject=' + subject + '&body=' + body;
}

function evaluationMailto() {
  const subject = encodeURIComponent('Independent agent evaluation');
  const body = encodeURIComponent(
    [
      'I would like to start an independent agent evaluation ($8,000, two weeks, one agent).',
      '',
      'Organization:',
      'Agent or agent system:',
      'Intended use:',
      'Access (the agent, or a staging copy):',
      'Tests we already run:',
    ].join('\n')
  );
  return 'mailto:' + CONTACT_EMAIL + '?subject=' + subject + '&body=' + body;
}

document.querySelectorAll('[data-mailto="commercial"]').forEach(function (el) {
  el.setAttribute('href', commercialMailto());
});

document.querySelectorAll('[data-mailto="evaluation"]').forEach(function (el) {
  el.setAttribute('href', evaluationMailto());
});

const header = document.querySelector('.site-header');
const toggle = document.querySelector('.nav-toggle');
const nav = document.getElementById('site-nav');

if (toggle && header) {
  toggle.addEventListener('click', function () {
    const open = header.classList.toggle('open');
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  });
  nav.querySelectorAll('a').forEach(function (link) {
    link.addEventListener('click', function () {
      header.classList.remove('open');
      toggle.setAttribute('aria-expanded', 'false');
      toggle.setAttribute('aria-label', 'Open menu');
    });
  });
}

document.querySelectorAll('#faq details').forEach(function (item) {
  item.addEventListener('toggle', function () {
    if (!item.open) return;
    document.querySelectorAll('#faq details').forEach(function (other) {
      if (other !== item) other.open = false;
    });
  });
});
