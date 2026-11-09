const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
function element(extra = {}) { return {textContent: '', events: {}, dataset: {}, addEventListener(k, f) {this.events[k] = f;}, ...extra}; }
{
  const options = [element({value:'Sold', disabled:true, dataset:{price:'50',stock:'0'}}), element({value:'Standard', dataset:{price:'12.5',stock:'3'}}), element({value:'VIP', dataset:{price:'30',stock:'20'}})];
  const select = element({options, value:'Sold'});
  Object.defineProperty(select, 'selectedOptions', {get() {return options.filter(x => x.value === this.value);}});
  const nodes = {checkoutForm: element({dataset:{maxOrder:'10'}}), ticket_type:select, quantity:element({value:'1'}), orderQuantity:element(), quantityHint:element(), total_display:element()};
  vm.runInNewContext(fs.readFileSync('static/js/checkout.js','utf8'), {document:{getElementById:k=>nodes[k]}});
  assert.equal(select.value,'Standard');
  assert.equal(nodes.total_display.textContent,'$12.50');
  nodes.quantity.value = '99'; nodes.quantity.events.input();
  assert.equal(nodes.quantity.value,3);
  assert.equal(nodes.total_display.textContent,'$37.50');
  select.value = 'VIP'; select.events.change();
  nodes.quantity.value = '99'; nodes.quantity.events.input();
  assert.equal(nodes.quantity.value,10);
  assert.equal(nodes.total_display.textContent,'$300.00');
  nodes.quantity.value = '-2'; nodes.quantity.events.input();
  assert.equal(nodes.quantity.value,1);
  options.forEach(x=>x.disabled=true); select.events.change();
  assert.equal(nodes.total_display.textContent,'$0.00');
}
function slideshow(reduced) {
  const slides = Array.from({length:3},()=>element({classList:{active:false,toggle(k,v){this.active=v;}}}));
  const dots = slides.map((s,i)=>element({dataset:{slide:String(i)},setAttribute(k,v){this[k]=v;}}));
  const controls=element({hidden:true}), toggle=element();
  const hero=element({contains(){return false;},querySelectorAll(s){return s==='.hero-slide'?slides:dots;},querySelector(){return controls;}});
  hero.querySelector=s=>s==='.hero-controls'?controls:toggle;
  const media=element({matches:reduced});
  let callback=null;
  const document=element({hidden:false,activeElement:null,getElementById(){return hero;}});
  vm.runInNewContext(fs.readFileSync('static/js/hero-slideshow.js','utf8'),{
    document, window:{matchMedia(){return media;}}, clearInterval(){callback=null;}, setInterval(fn,ms){assert.equal(ms,6000);callback=fn;return 1;},setTimeout(fn){fn();}
  });
  return {hero,slides,dots,toggle,document,tick(){assert.ok(callback);callback();},playing(){return !!callback;}};
}
{
  const ui=slideshow(false);
  assert.ok(ui.playing());ui.tick();assert.ok(ui.slides[1].classList.active);
  ui.toggle.events.click();assert.equal(ui.playing(),false);
  ui.toggle.events.click();assert.equal(ui.playing(),true);
  ui.document.hidden=true;ui.document.events.visibilitychange();assert.equal(ui.playing(),false);
  ui.document.hidden=false;ui.document.events.visibilitychange();assert.equal(ui.playing(),true);
  ui.dots[2].events.click();assert.equal(ui.playing(),false);assert.ok(ui.slides[2].classList.active);
  const reduced=slideshow(true);assert.equal(reduced.playing(),false);
}
console.log('Public UI checks passed: price totals, inventory limits, slideshow controls, hidden tabs and reduced motion.');
