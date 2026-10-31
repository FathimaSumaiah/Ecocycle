function saveProfile(){

    const newName = document.getElementById("nameInput").value.trim();
    const newPhone = document.getElementById("phoneInput").value.trim();

    if(newName.length < 2){
        toast("⚠️ Name must be at least 2 characters");
        return;
    }

    if(!/^[0-9]{10}$/.test(newPhone)){
        toast("⚠️ Enter a valid 10-digit phone number");
        return;
    }

    fetch("/api/household/update-profile",{
        method:"POST",
        headers:{
            "Content-Type":"application/json"
        },
        body:JSON.stringify({
            name:newName,
            phone:newPhone
        })
    })
    .then(r=>r.json())
    .then(data=>{

        if(data.success){

            toast("✅ Profile updated successfully");

            document.querySelector(".profile-header h1").textContent=newName;

            document.querySelectorAll(".avatar-circle,.profile-avatar-large")
            .forEach(e=>{
                e.textContent=newName.slice(0,2).toUpperCase();
            });

        }else{
            toast("❌ Update failed");
        }

    });

}

function toast(msg){
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 3500);
}

// Location Map - Leaflet
let map, marker;
const DEFAULT_LAT = 11.2588;
const DEFAULT_LNG = 75.7804;

function initMap(){
  const latlngText = document.getElementById('latlngText').textContent.trim();
  let lat = DEFAULT_LAT, lng = DEFAULT_LNG;
  try {
    const parts = latlngText.split(',');
    if(parts.length===2){
      lat = parseFloat(parts[0]); lng = parseFloat(parts[1]);
    }
  } catch(e){}

  map = L.map('map').setView([lat, lng], 15);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap'
  }).addTo(map);
  marker = L.marker([lat, lng]).addTo(map)
    .bindPopup(`House Location<br>Ward ${lat}, ${lng}`).openPopup();
}

function openInMaps(){
  const txt = document.getElementById('latlngText').textContent.trim();
  window.open(`https://www.google.com/maps/search/?api=1&query=${txt}`, '_blank');
}
function getDirections(){
  const txt = document.getElementById('latlngText').textContent.trim();
  window.open(`https://www.google.com/maps/dir/?api=1&destination=${txt}`, '_blank');
}
function copyLocation(){
  const txt = document.getElementById('latlngText').textContent.trim();
  navigator.clipboard.writeText(txt).then(()=>toast('📋 Coordinates copied: ' + txt));
}

document.addEventListener('DOMContentLoaded', initMap);
