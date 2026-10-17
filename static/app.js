const $ = (s) => document.querySelector(s);
let posts = [];
let selected = new Set();

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

const recipientSearch = $("#recipient-search");
if (recipientSearch) {
  recipientSearch.addEventListener("input", () => {
    const q = recipientSearch.value.toLowerCase();
    document.querySelectorAll(".recipient-row").forEach(row => {
      row.style.display = row.innerText.toLowerCase().includes(q) ? "" : "none";
    });
  });
}

function renderPosts() {
  const root = $("#posts-list");
  if (!root) return;
  const q = ($("#post-search")?.value || "").toLowerCase();
  const shown = posts.filter(p => `${p.date} ${p.type} ${p.text} ${p.id}`.toLowerCase().includes(q));
  if (!shown.length) {
    root.innerHTML = '<div class="empty big-empty">Mos post topilmadi.</div>';
  } else {
    root.innerHTML = shown.map(p => `
      <label class="post-card ${selected.has(p.id) ? "selected" : ""}">
        <div class="post-top">
          <input class="post-check" type="checkbox" data-id="${p.id}" ${selected.has(p.id) ? "checked" : ""}>
          <span class="post-type">${escapeHtml(p.type)}</span>
          <span class="post-date">${escapeHtml(p.date)}</span>
        </div>
        <p>${escapeHtml(p.text || "[Matn yo‘q]")}</p>
        <span class="post-id">Saved Messages · #${p.id}</span>
      </label>`).join("");
    root.querySelectorAll("input[data-id]").forEach(box => {
      box.addEventListener("change", () => {
        const id = Number(box.dataset.id);
        if (box.checked) {
          if (selected.size >= 10) {
            box.checked = false;
            alert("Bir yuborishda ko‘pi bilan 10 ta post tanlash mumkin.");
            return;
          }
          selected.add(id);
        } else selected.delete(id);
        renderPosts();
        updateSelection();
      });
    });
  }
  updateSelection();
}

function updateSelection() {
  const count = $("#selected-count");
  const send = $("#send-posts");
  if (count) count.textContent = `${selected.size} ta post tanlandi`;
  if (send) send.disabled = selected.size === 0;
}

async function loadPosts() {
  const status = $("#post-status");
  const button = $("#load-posts");
  if (!status || !button) return;
  button.disabled = true;
  status.innerHTML = "<b>Yuklanmoqda</b><span>Telegram Saved Messages tekshirilmoqda…</span>";
  try {
    const res = await fetch("/api/posts");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Postlarni yuklab bo‘lmadi.");
    posts = data;
    selected.clear();
    renderPosts();
    status.innerHTML = `<b>Tayyor</b><span>${posts.length} ta post topildi. Keraklilarini belgilang.</span>`;
  } catch (e) {
    status.innerHTML = `<b>Xatolik</b><span>${escapeHtml(e.message)}</span>`;
  } finally {
    button.disabled = false;
  }
}

const loadButton = $("#load-posts");
if (loadButton) loadButton.addEventListener("click", loadPosts);
const searchPosts = $("#post-search");
if (searchPosts) searchPosts.addEventListener("input", renderPosts);

const modal = $("#confirm-modal");
const sendButton = $("#send-posts");
const closeModal = $("#close-modal");
const consent = $("#consent-check");
const confirmButton = $("#confirm-send");

if (sendButton) sendButton.addEventListener("click", () => {
  const chosen = posts.filter(p => selected.has(p.id));
  $("#confirm-summary").textContent = `${chosen.length} ta post barcha faol klientlarga yuboriladi. Manba ko‘rsatilmaydi — xabarlar yangi xabar sifatida yuboriladi.`;
  $("#confirm-items").innerHTML = chosen.map(p => `<div>• ${escapeHtml(p.type)} · ${escapeHtml(p.date)} · ${escapeHtml(p.text || "[Matn yo‘q]")}</div>`).join("");
  consent.checked = false;
  confirmButton.disabled = true;
  modal.classList.remove("hidden");
});
if (closeModal) closeModal.addEventListener("click", () => modal.classList.add("hidden"));
if (modal) modal.addEventListener("click", e => { if (e.target === modal) modal.classList.add("hidden"); });
if (consent) consent.addEventListener("change", () => { confirmButton.disabled = !consent.checked; });


if (confirmButton) confirmButton.addEventListener("click", async () => {
  if (!consent.checked || !selected.size || confirmButton.dataset.sending === "1") {
    return;
  }

  const postIds = [...selected];
  confirmButton.dataset.sending = "1";
  confirmButton.disabled = true;
  confirmButton.textContent = "Yuborilmoqda…";

  try {
    const res = await fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        post_ids: postIds,
        confirm: true,
        consent: true
      })
    });

    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.error || `Server xatosi: ${res.status}`);
    }

    if (!Array.isArray(data.results)) {
      throw new Error(
        "Server yuborish natijalarini qaytarmadi. Tarixni tekshiring."
      );
    }

    const results = data.results;
    const sent = results.filter(x => x.status === "sent");
    const failed = results.filter(x => x.status === "failed");
    const other = results.filter(
      x => x.status !== "sent" && x.status !== "failed"
    );

    modal.classList.add("hidden");

    alert(
      `Yuborish jarayoni yakunlandi.\n\n` +
      `Natija qaytgan klientlar: ${results.length}\n` +
      `Muvaffaqiyatli deb qayd etilgan: ${sent.length}\n` +
      `Xato deb qayd etilgan: ${failed.length}\n` +
      `Holati noma'lum: ${other.length}\n\n` +
      `Bu raqamlar xabarlar o'qilganini yoki albatta yetkazilganini anglatmaydi.\n` +
      `Har bir klient natijasini Yuborish tarixi bo'limida tekshiring.`
    );

    window.location.href = "/history";
  } catch (e) {
    alert(
      `Yuborish natijasi to'liq olinmadi.\n\n${e.message}\n\n` +
      `Takror yuborishga shoshilmang: ayrim xabarlar allaqachon ketgan bo'lishi mumkin. Tarixni tekshiring.`
    );
  } finally {
    delete confirmButton.dataset.sending;
    confirmButton.textContent = "Yuborishni boshlash";
    confirmButton.disabled = !consent.checked;
  }
});



document.addEventListener("change", function (event) {
  if (event.target.id !== "select-all-recipients") return;

  const checked = event.target.checked;

  document
    .querySelectorAll(".recipient-row input[type='checkbox']")
    .forEach(function (checkbox) {
      checkbox.checked = checked;
    });
});

document.addEventListener("change", function (event) {
  if (!event.target.matches(".recipient-row input[type='checkbox']")) return;

  const boxes = Array.from(
    document.querySelectorAll(".recipient-row input[type='checkbox']")
  );

  const master = document.querySelector("#select-all-recipients");
  if (!master) return;

  const checkedCount = boxes.filter(box => box.checked).length;

  master.checked = boxes.length > 0 && checkedCount === boxes.length;
  master.indeterminate = checkedCount > 0 && checkedCount < boxes.length;
});

