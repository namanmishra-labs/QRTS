(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const status = document.querySelector('#qr-status');
  const list = document.querySelector('#qr-list');
  document.querySelector('#print-page').addEventListener('click', () => window.print());
  fetch('/api/attendees/qr', {
    method: 'POST', headers: {'X-CSRF-Token': csrf}, credentials: 'same-origin'
  })
    .then(async response => {
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Could not load attendee QR codes.');
      return payload.attendees;
    })
    .then(attendees => {
      if (!window.QRCode) throw new Error('QR generator did not load. Check your internet connection and reload.');
      status.hidden = true;
      if (!attendees.length) { status.hidden = false; status.textContent = 'No attendee records were found.'; return; }
      for (const attendee of attendees) {
        const item = document.createElement('article'); item.className = 'qr-item';
        const heading = document.createElement('h2'); heading.textContent = attendee.name;
        const id = document.createElement('p'); id.textContent = attendee.registration_id;
        const image = document.createElement('div'); image.className = 'qr-image';
        const qr = new QRCode(image, {text: attendee.qr_token, width: 144, height: 144, correctLevel: QRCode.CorrectLevel.M});
        const download = document.createElement('button'); download.className = 'secondary'; download.textContent = 'Download QR';
        download.addEventListener('click', () => {
          const source = image.querySelector('img')?.src || image.querySelector('canvas')?.toDataURL('image/png');
          if (!source) return;
          const anchor = document.createElement('a'); anchor.href = source;
          anchor.download = `${attendee.registration_id.replace(/[^A-Za-z0-9_-]/g, '_')}-qr.png`; anchor.click();
        });
        item.append(heading, id, image, download); list.append(item);
      }
    })
    .catch(error => { status.textContent = error.message; status.className = 'notice error'; });
})();
