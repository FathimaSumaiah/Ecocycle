// DATA
// const wards = [
//   { id:'W-01', workers:10, pend:4, appr:7, coll:31 },
//   { id:'W-02', workers:10, pend:6, appr:5, coll:27 },
//   { id:'W-03', workers:10, pend:2, appr:4, coll:34 },
//   { id:'W-04', workers:10, pend:9, appr:6, coll:24 },
// ];
// const pending = [
//   { name:'Meera Nair', ward:'W-02', house:'14/B', phone:'98470 XXXX1', time:'2h ago' },
//   { name:'Abhilash P.', ward:'W-01', house:'7', phone:'94950 XXXX8', time:'3h ago' },
//   { name:'Fathima K.', ward:'W-04', house:'22/A', phone:'96330 XXXX4', time:'5h ago' },
// ];
// const workers = [
//   { name:'Rajeev K.', ward:'W-01', comp:9, rej:1, status:'On track', cls:'ok' },
//   { name:'Sini Thomas', ward:'W-02', comp:6, rej:2, status:'Behind pace', cls:'warn' },
//   { name:'Anoop M.', ward:'W-03', comp:11, rej:0, status:'On track', cls:'ok' },
//   { name:'Divya S.', ward:'W-04', comp:4, rej:3, status:'Needs review', cls:'bad' },
// ];
// const rewards = [
//   { name:'Meera Nair', pts:'18 pts' },
//   { name:'Suresh Babu', pts:'32 pts' },
//   { name:'Anitha R.', pts:'9 pts' },
// ];

function toast(msg){
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2000);
}

function render(){
  // Ward
  document.getElementById('ward-body').innerHTML = wards.map(w=>{
    const total=w.pend+w.appr+w.coll;
    return `<tr>
      <td><span class="ward-pill">${w.id}</span></td>
      <td class="mono">${w.workers} workers</td>
      <td><div class="flow-bar"><i class="p" style="width:${w.pend/total*100}%"></i><i class="a" style="width:${w.appr/total*100}%"></i><i class="c" style="width:${w.coll/total*100}%"></i></div></td>
      <td class="mono">${w.pend} pend · ${w.appr} appr · ${w.coll} coll</td>
    </tr>`;
  }).join('');

  document.getElementById('pending-body').innerHTML = pending.map(p=>`
    <tr>
      <td>${p.name}</td><td class="mono">${p.ward}</td><td>${p.house}</td><td class="mono">${p.phone}</td>
      <td><a class="link" onclick="toast('Aadhaar photo preview')">View photo</a></td><td class="mono">${p.time}</td>
      <td><button class="btn btn-approve" onclick="handleApprove('${p.name}')">APPROVE</button> <button class="btn btn-reject" onclick="handleReject('${p.name}')">REJECT</button></td>
    </tr>`).join('');

  document.getElementById('worker-body').innerHTML = workers.map(w=>`
    <tr><td>${w.name}</td><td class="mono">${w.ward}</td><td>${w.comp}</td><td>${w.rej}</td><td><span class="status ${w.cls}">${w.status}</span></td></tr>
  `).join('');

  document.getElementById('rewards-body').innerHTML = rewards.map(r=>`
    <tr><td>${r.name}</td><td class="mono">${r.pts}</td><td><button class="btn btn-approve" onclick="toast('Reward approved for ${r.name}')">APPROVE</button></td></tr>
  `).join('');
}

function handleApprove(name){ toast(`Approved ${name}`); }
function handleReject(name){ toast(`Rejected ${name}`); }

render();
