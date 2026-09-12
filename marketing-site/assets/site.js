const phone = '529613632496';
const message = encodeURIComponent('Hola, quiero conocer FdezNet para administrar mi ISP.');
document.querySelectorAll('[data-whatsapp]').forEach((link) => {
  link.href = `https://wa.me/${phone}?text=${message}`;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
});
document.querySelectorAll('[data-year]').forEach((node) => { node.textContent = new Date().getFullYear(); });

const menuButton = document.querySelector('.menu-button');
const nav = document.querySelector('.main-nav');
menuButton?.addEventListener('click', () => {
  const open = menuButton.getAttribute('aria-expanded') === 'true';
  menuButton.setAttribute('aria-expanded', String(!open));
  nav?.classList.toggle('open', !open);
});
nav?.querySelectorAll('a').forEach((link) => link.addEventListener('click', () => {
  menuButton?.setAttribute('aria-expanded', 'false');
  nav.classList.remove('open');
}));

const observer = new IntersectionObserver((entries) => {
  entries.forEach((entry) => { if (entry.isIntersecting) entry.target.classList.add('visible'); });
}, { threshold: 0.08 });
document.querySelectorAll('.reveal').forEach((element) => observer.observe(element));
