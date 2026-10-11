"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]').content;
const list = document.getElementById("candidates");
const message = document.getElementById("message");

async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify(body),
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* handled below */ }
  if (!response.ok) throw new Error(payload.error || "Request failed. Reload and try again.");
  return payload;
}

function showMessage(text, isError = false) {
  message.textContent = text;
  message.className = isError ? "notice warning" : "notice";
  message.hidden = false;
}

async function loadCandidates() {
  list.replaceChildren();
  showMessage("Loading the first three eligible test rows…");
  try {
    const data = await post("/api/qr-email/candidates");
    if (!data.candidates.length) {
      list.textContent = "No eligible rows. Add test data, an existing QR token, and the three email tracking columns.";
    }
    data.candidates.forEach((candidate) => {
      const card = document.createElement("article");
      card.className = "email-candidate";
      const heading = document.createElement("h3");
      heading.textContent = `${candidate.name} · ${candidate.registration_id}`;
      const status = document.createElement("p");
      status.textContent = `Status: ${candidate.email_status}${candidate.email_timestamp ? ` · ${candidate.email_timestamp}` : ""}`;
      card.append(heading, status);
      if (candidate.email_error) {
        const error = document.createElement("p");
        error.textContent = `Safe error code: ${candidate.email_error}`;
        card.append(error);
      }
      if (["Not sent", "Unknown"].includes(candidate.email_status)) {
        const button = document.createElement("button");
        button.className = "primary";
        button.textContent = "Send this attendee’s QR email";
        button.addEventListener("click", async () => {
          const confirmed = window.confirm(`Send one individual QR email to the address stored on this row for ${candidate.name}? This cannot be undone.`);
          if (!confirmed) return;
          button.disabled = true;
          try {
            await post("/api/qr-email/send", { row_number: candidate.row_number, confirm_send: true });
            showMessage("Email sent. Reloading the test-row status.");
            await loadCandidates();
          } catch (error) {
            showMessage(error.message, true);
            button.disabled = false;
            await loadCandidates();
          }
        });
        card.append(button);
      }
      list.append(card);
    });
    message.hidden = true;
  } catch (error) {
    showMessage(error.message, true);
  }
}

document.getElementById("reload").addEventListener("click", loadCandidates);
loadCandidates();
