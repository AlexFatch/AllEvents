(() => {
  const hero = document.getElementById('heroSlideshow');
  if (!hero) return;
  const slides = [...hero.querySelectorAll('.hero-slide')];
  const controls = hero.querySelector('.hero-controls');
  const dots = [...hero.querySelectorAll('[data-slide]')];
  const toggle = hero.querySelector('.hero-toggle');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let current = 0, timer = null, paused = reducedMotion.matches, hovered = false;

  function show(index) {
    current = index;
    slides.forEach((slide, i) => slide.classList.toggle('is-active', i === current));
    dots.forEach((dot, i) => dot.setAttribute('aria-pressed', String(i === current)));
  }
  function schedule() {
    clearInterval(timer);
    toggle.textContent = paused ? 'Play slideshow' : 'Pause slideshow';
    if (!paused && !document.hidden && !hovered && !hero.contains(document.activeElement)) {
      timer = setInterval(() => show((current + 1) % slides.length), 6000);
    }
  }
  dots.forEach(dot => dot.addEventListener('click', () => {
    show(Number(dot.dataset.slide));
    paused = true;
    schedule();
  }));
  toggle.addEventListener('click', () => { paused = !paused; schedule(); });
  hero.addEventListener('mouseenter', () => { hovered = true; schedule(); });
  hero.addEventListener('mouseleave', () => { hovered = false; schedule(); });
  hero.addEventListener('focusin', schedule);
  hero.addEventListener('focusout', () => setTimeout(schedule, 0));
  document.addEventListener('visibilitychange', schedule);
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches) paused = true;
    schedule();
  });
  controls.hidden = slides.length < 2;
  show(0);
  schedule();
})();
