/* Chat widget.
   The page never sends a booking reference — only the opaque token the server
   issued when it rendered this trip. Whatever the agent learns about who this
   is, it learns from the server, not from anything editable in the DOM. */

const token = document.body.dataset.token;
const panel = document.querySelector('.panel');
const launcher = document.querySelector('.launcher');
const log = document.getElementById('log');
const chips = document.getElementById('chips');
const form = document.getElementById('composer');
const input = document.getElementById('input');
const send = form.querySelector('.send');

let opened = false;
let busy = false;

function openChat(seed) {
  panel.hidden = false;
  launcher.hidden = true;
  if (!opened) {
    opened = true;
    addAgent(
      "Hello. I can see this trip, so you won't need to give me your booking details.\n\n" +
      "I can look for another flight, sort out vouchers, or tell you what you're entitled to."
    );
  }
  if (seed) { input.value = seed; }
  input.focus();
  autosize();
}

function closeChat() {
  panel.hidden = true;
  launcher.hidden = false;
}

document.querySelectorAll('[data-open-chat]').forEach((el) => {
  el.addEventListener('click', () => openChat(el.dataset.seed));
});
document.querySelector('[data-close-chat]').addEventListener('click', closeChat);

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !panel.hidden) closeChat();
});

/* Deliberately tiny: bold, bullets, paragraphs. Anything richer would mean
   trusting model output as markup, and the payoff does not justify it. */
function render(text) {
  const esc = text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc
    .split(/\n{2,}/)
    .map((block) => {
      const lines = block.split('\n');
      const bulleted = lines.every((l) => /^\s*[-*•]\s+/.test(l));
      const body = (s) => s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      if (bulleted) {
        return '<ul>' + lines
          .map((l) => '<li>' + body(l.replace(/^\s*[-*•]\s+/, '')) + '</li>')
          .join('') + '</ul>';
      }
      return '<p>' + body(block.replace(/\n/g, '<br>')) + '</p>';
    })
    .join('');
}

function addAgent(text, tools) {
  const el = document.createElement('div');
  el.className = 'msg agent';
  el.innerHTML = render(text);
  if (tools && tools.length) {
    const trace = document.createElement('div');
    trace.className = 'trace';
    // Shown on purpose: the demo claims the agent really calls the airline,
    // and this is the receipt.
    [...new Set(tools)].forEach((t) => {
      const s = document.createElement('span');
      s.textContent = t;
      trace.appendChild(s);
    });
    el.appendChild(trace);
  }
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function addUser(text) {
  const el = document.createElement('div');
  el.className = 'msg user';
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function showTyping() {
  const el = document.createElement('div');
  el.className = 'msg agent typing';
  el.innerHTML = '<i></i><i></i><i></i>';
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

async function ask(text) {
  if (busy || !text.trim()) return;
  busy = true;
  send.disabled = true;
  chips.style.display = 'none';
  addUser(text);
  const typing = showTyping();

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, message: text }),
    });
    const data = await res.json();
    typing.remove();
    addAgent(data.reply || '…', data.tools);
    if (data.trip_changed) {
      const code = (data.reply.match(/\b([A-Z0-9]{6})\b/) || [])[1];
      refreshTrip(code);
    }
  } catch (err) {
    typing.remove();
    addAgent("I couldn't reach our systems just then. Try again in a moment.");
  } finally {
    busy = false;
    send.disabled = false;
  }
}

chips.addEventListener('click', (e) => {
  const chip = e.target.closest('.chip');
  if (chip) ask(chip.textContent);
});

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = input.value;
  input.value = '';
  autosize();
  ask(text);
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

function autosize() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 108) + 'px';
}
input.addEventListener('input', autosize);

/* Keeping the page honest.
   The chat can change the trip the page is displaying. Leaving the card on its
   original render means the screen contradicts itself — still CANCELLED beside
   a conversation that just rebooked you — and the passenger has no way to know
   which half to trust. So after a rebooking the page re-reads its own state
   from the server rather than guessing from the reply text. */

async function refreshTrip(confirmationCode) {
  try {
    const res = await fetch('/api/trip?token=' + encodeURIComponent(token));
    if (!res.ok) return;
    const trip = await res.json();

    document.querySelectorAll('[data-f]').forEach((el) => {
      const key = el.dataset.f;
      if (trip[key] === undefined) return;
      el.textContent = key === 'status' ? trip.status_label : trip[key];
      if (key === 'status') el.className = 'status ' + trip.status_class;
    });

    const slot = document.getElementById('banner-slot');
    if (slot && !trip.disrupted) {
      slot.innerHTML =
        '<div class="confirm-note"><strong>Rebooked.</strong> Your trip below is up to date.' +
        (confirmationCode ? ' New confirmation <code>' + confirmationCode + '</code>' : '') +
        '</div>';
    }

    const pass = document.getElementById('pass');
    pass.classList.remove('updated');
    void pass.offsetWidth;          // restart the animation
    pass.classList.add('updated');
  } catch (err) {
    /* The card simply stays as it was; the chat already told them what happened. */
  }
}
