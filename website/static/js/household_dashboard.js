async function requestPickup(){
  const btn = document.getElementById('requestBtn');
  if(!btn || btn.disabled) return;

  // CONFIRMATION POPUP
  const ok = confirm("Confirm Pickup Request?\n");
  if(!ok) return;

  const originalText = btn.innerText;
  btn.disabled = true;
  btn.innerText = "REQUESTING...";
  
  try{
    const res = await fetch('/request_pickup', {method:'POST'});
    const data = await res.json();
    if(data.limit_reached){
      alert("Oops sorry, worker limit reached, please comeback tomorrow");
      btn.disabled = false;
      btn.innerText = originalText;
      return;
    }
    alert(data.message);
    if(data.success){
      location.reload();
    } else {
      btn.disabled = false;
      btn.innerText = originalText;
    }
  } catch(err){
    console.error(err);
    alert("Server error - check terminal");
    btn.disabled = false;
    btn.innerText = originalText;
  }
}

async function cancelPickup(){
  if(!confirm("Cancel this pickup?")) return;
  const res = await fetch('/cancel_pickup', {method:'POST'});
  const data = await res.json();
  alert(data.message);
  if(data.success) location.reload();
}

function showPending(data){
  const box = document.getElementById('pendingBox');
  if(!box) return;
  box.innerHTML = `<div class="pending-item"><div><b>${data.kg}kg Pickup</b><div class="mono-small">${data.time}</div></div><span class="pending-status">${data.status}</span></div>`;
  document.getElementById('pendingCount').textContent = '1 Active';
}

function toast(msg){
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 4000);
}

function updateButtonState(){
  const now = new Date();
  const day = now.getDay(); // 0 = Sunday
  const hour = now.getHours();
  const minute = now.getMinutes();
  const badge = document.getElementById('timeBadge');
  const btn = document.getElementById('requestBtn');
  if(!badge || !btn) return;
  
  if(day === 0){
    // Sunday Holiday
    badge.style.background = '#8B3A2A';
    badge.textContent = `🔴 CLOSED - Today is Sunday (Holiday)`;
    badge.title = 'Sundays are holidays - Requests open Monday 8 AM';
    
    btn.disabled = true;
    btn.style.background = '#E6E0D0';
    btn.style.color = '#7A8A83';
    btn.textContent = 'SUNDAY HOLIDAY';
    btn.style.boxShadow = 'none';
    return;
  }

  if(hour >= 8 && hour < 16){
    // OPEN - Green
    badge.style.background = '#2A6B5E';
    badge.textContent = `🟢 Request OPEN: 8:00 AM - 4:00 PM | Now: ${now.toLocaleTimeString()}`;
    badge.title = 'You can request pickup now';
    
    btn.disabled = false;
    btn.style.background = '#2A6B5E'; // Green when open
    btn.style.color = '#fff';
    btn.textContent = 'REQUEST PICKUP - OPEN';
    btn.style.boxShadow = '0 4px 16px rgba(42,107,94,0.4)';
  } else {
    // CLOSED - Red/Grey
    badge.style.background = '#8B3A2A';
    if(hour < 8){
      badge.textContent = `🔴 CLOSED - Opens at 8 AM | Now: ${now.toLocaleTimeString()}`;
    } else {
      badge.textContent = `🔴 CLOSED - Opens tomorrow 8 AM | Now: ${now.toLocaleTimeString()}`;
    }
    badge.title = 'Requests allowed only 8 AM - 4 PM';
    
    btn.disabled = true;
    btn.style.background = '#E6E0D0'; // Grey when closed
    btn.style.color = '#7A8A83';
    btn.textContent = 'CLOSED - Opens 8 AM';
    btn.style.boxShadow = 'none';
  }
}

// Auto-update every minute, so at 8:00 AM it automatically turns green
document.addEventListener('DOMContentLoaded', ()=>{
  updateButtonState(); // initial check
  setInterval(updateButtonState, 60000); // check every 60 seconds automatically
  // Also check every second for exact 8:00:00 transition if user keeps page open
  setInterval(updateButtonState, 1000);
  
  
});
async function cancelPickup(){
  if(!confirm('Cancel pickup request?')) return;
  const res = await fetch('/cancel_pickup', {method:'POST', headers:{'X-Requested-With':'XMLHttpRequest'}});
  const data = await res.json();
  if(data.success){
    document.getElementById('pendingBox').innerHTML = `
      <div class="pending-empty">
        <div class="empty-icon">📦</div>
        <h4>No active pickup request</h4>
        <p class="mono-small">Pickup cancelled.</p>
      </div>`;
    toast('Pickup cancelled');
  } else {
    alert(data.message);
  }
}