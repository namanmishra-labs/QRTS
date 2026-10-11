"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]').content;
const page = document.querySelector(".qr-email-management");
const rows = document.getElementById("candidate-rows");
const message = document.getElementById("message");
const selectAll = document.getElementById("select-all");
const buttons = ["send-all", "send-selected", "retry-failed", "reload"]
  .map((id) => document.getElementById(id));
let candidates = [];
let busy = false;

async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* report a generic safe error */ }
  if (!response.ok) throw new Error(payload.error || "Request failed. Reload and try again.");
  return payload;
}

function showMessage(text, isError = false) {
  message.textContent = text;
  message.className = isError ? "notice error" : "notice";
  message.hidden = false;
}

function sendable(candidate) {
  return candidate.email_valid && ["Pending", "Failed"].includes(candidate.email_status);
}

function syncControls() {
  buttons.forEach((button) => { button.disabled = busy; });
  const checkboxes = [...rows.querySelectorAll("input[data-registration-id]")];
  const eligible = checkboxes.filter((input) => !input.disabled);
  selectAll.disabled = busy || eligible.length === 0;
  selectAll.checked = eligible.length > 0 && eligible.every((input) => input.checked);
  document.getElementById("send-selected").disabled = busy ||
    !checkboxes.some((input) => input.checked && !input.disabled);
  document.getElementById("retry-failed").disabled = busy ||
    !candidates.some((candidate) => candidate.email_valid && candidate.email_status === "Failed");
}

function renderCandidates() {
  rows.replaceChildren();
  candidates.forEach((candidate) => {
    const row = document.createElement("tr");
    const selectCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.registrationId = candidate.registration_id;
    checkbox.setAttribute("aria-label", `Select ${candidate.name} (${candidate.registration_id})`);
    checkbox.disabled = busy || !sendable(candidate);
    checkbox.addEventListener("change", syncControls);
    selectCell.append(checkbox);
    const tracking = [candidate.email_status,
      candidate.email_timestamp,
      candidate.email_error ? `Error: ${candidate.email_error}` : ""]
      .filter(Boolean).join(" · ");
    const values = [candidate.registration_id, candidate.name, candidate.email,
      candidate.email_valid ? tracking || "Pending" : "Invalid email · skipped"];
    row.append(selectCell);
    values.forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value || "—";
      row.append(cell);
    });
    rows.append(row);
  });
  syncControls();
}

async function loadCandidates() {
  rows.replaceChildren();
  showMessage("Loading attendee email status…");
  try {
    const data = await post("/api/qr-email/candidates");
    candidates = Array.isArray(data.candidates) ? data.candidates : [];
    renderCandidates();
    if (!candidates.length) {
      showMessage(page.dataset.testMode === "true"
        ? "No eligible test recipients found. Add approved test rows and verify the email tracking columns."
        : "No attendees are available for QR email processing.");
    } else {
      message.hidden = true;
    }
  } catch (error) {
    candidates = [];
    renderCandidates();
    showMessage(error.message, true);
  }
}

function registrationIdsSelected() {
  return [...rows.querySelectorAll("input[data-registration-id]:checked")]
    .map((input) => input.dataset.registrationId);
}

function targetsFor(action) {
  if (action === "selected") {
    const selected = new Set(registrationIdsSelected());
    return candidates.filter((candidate) => selected.has(candidate.registration_id) && sendable(candidate));
  }
  if (action === "retry_failed") {
    return candidates.filter((candidate) => candidate.email_status === "Failed");
  }
  return candidates.filter((candidate) => ["Pending", "Failed"].includes(candidate.email_status));
}

async function runAction(action) {
  if (busy) return;
  const targets = targetsFor(action);
  if (!targets.length) {
    showMessage(action === "selected" ? "Select at least one eligible attendee." : "There are no eligible recipients for this action.", true);
    return;
  }
  const actionLabel = action === "all" ? "Send QR emails to all eligible attendees"
    : action === "retry_failed" ? "Retry the failed QR email attempts"
      : "Send QR emails to the selected attendees";
  const testLimit = page.dataset.testMode === "true" ? " Test Mode caps this operation at 3 recipients." : "";
  const confirmed = window.confirm(`${actionLabel} for ${targets.length} recipient(s)? Previously sent emails will be skipped.${testLimit}`);
  if (!confirmed) return;

  busy = true;
  renderCandidates();
  showMessage("Processing the confirmed email batch. Keep this page open…");
  const totals = {selected_count: 0, eligible_count: 0, processed_count: 0,
    sent_count: 0, failed_count: 0, skipped_count: 0, manual_review_count: 0};
  let confirmedCount = 0;
  try {
    for (let offset = 0; offset < targets.length; offset += 3) {
      const group = targets.slice(offset, offset + 3);
      const result = await post("/api/qr-email/batch", {
        action: "selected",
        registration_ids: group.map((candidate) => candidate.registration_id),
        confirm_send: true,
      });
      Object.keys(totals).forEach((key) => { totals[key] += Number(result[key]) || 0; });
      confirmedCount += group.length;
      showMessage(`Processed ${Math.min(offset + group.length, targets.length)} of ${targets.length} selected recipients…`);
    }
    const prefix = `${targets.length} selected; ${totals.eligible_count} eligible; ${totals.sent_count} accepted by email service, ${totals.failed_count} failed, ${totals.skipped_count} skipped`;
    const review = totals.manual_review_count ? `, ${totals.manual_review_count} need manual review` : "";
    showMessage(`${prefix}${review}. Provider acceptance does not confirm inbox delivery.`,
      totals.failed_count > 0 || totals.manual_review_count > 0);
    await loadCandidates();
    if (totals.failed_count > 0 || totals.manual_review_count > 0) {
      showMessage(`${prefix}${review}. Provider acceptance does not confirm inbox delivery.`, true);
    }
  } catch (error) {
    const prior = `${totals.sent_count} accepted, ${totals.failed_count} failed, ${totals.skipped_count} skipped`;
    const uncertain = targets.length - confirmedCount;
    showMessage(`Batch interrupted after ${confirmedCount} confirmed recipient(s): ${prior}. ${uncertain} recipient(s) remain or need status review. Reload status before retrying. ${error.message}`, true);
    await loadCandidates();
    showMessage(`Batch interrupted after ${confirmedCount} confirmed recipient(s): ${prior}. ${uncertain} recipient(s) remain or need status review. Reload status before retrying.`, true);
  } finally {
    busy = false;
    renderCandidates();
  }
}

document.getElementById("send-all").addEventListener("click", () => runAction("all"));
document.getElementById("send-selected").addEventListener("click", () => runAction("selected"));
document.getElementById("retry-failed").addEventListener("click", () => runAction("retry_failed"));
document.getElementById("reload").addEventListener("click", loadCandidates);
selectAll.addEventListener("change", () => {
  rows.querySelectorAll("input[data-registration-id]").forEach((input) => {
    if (!input.disabled) input.checked = selectAll.checked;
  });
  syncControls();
});
loadCandidates();
