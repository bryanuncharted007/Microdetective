'use strict';
/* Microdetectives V3: online mode is enabled ONLY by a reachable first-party server.
 * In file/offline mode all points remain local practice and are never imported as prize points.
 */
(function(){
const el = s=>document.querySelector(s);
const esc = s=>String(s??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const state={live:false,player:null,room:null,poll:null,practiceLedger:{},allTiers:[]};
const fmt=n=>Number(n||0).toLocaleString('es-EC');
try{state.practiceLedger=JSON.parse(localStorage.getItem('micro-v3-awards')||'{}')}catch{}
const persist=()=>{try{localStorage.setItem('micro-v3-awards',JSON.stringify(state.practiceLedger))}catch{}};
async function api(path, opts={}){
  const response=await fetch('/api'+path,{method:opts.method||'GET',credentials:'same-origin',headers:{'Content-Type':'application/json',...(opts.headers||{})},body:opts.body===undefined?undefined:JSON.stringify(opts.body)});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:`No se pudo conectar (${response.status})`);
  return data;
}
function notify(s){if(typeof toast==='function')toast(s);else window.alert(s)}
function setStatus(text){el('#online-status').textContent=text}
function setScoreView(n,isOfficial){el('#total-score').textContent=fmt(n);el('#score-kind').textContent=isOfficial?'puntos oficiales':'puntos de práctica';el('#total-score').closest('.game-counter').classList.toggle('official',isOfficial)}
function paintProfile(p){
  state.player=p;
  el('#profile-title').textContent=`🔎 ${p.nickname}`;
  el('#profile-message').textContent='Tus puntos oficiales se conservan en este navegador y se reflejan en la tabla compartida. Puedes volver con el mismo dispositivo.';
  el('#my-position').textContent=`#${p.rank}`;
  el('#my-points').textContent=fmt(p.points);
  el('#player-nickname').value=p.nickname;
  el('#player-form').querySelector('button').textContent='Cambiar apodo';
  setScoreView(p.points,true);
}
function paintLocal(){
  el('#profile-title').textContent='🔎 Investigador en práctica';
  el('#profile-message').textContent='Estás probando una versión local. Tus puntos quedan en este navegador, pero todavía no sirven para premios ni para clasificaciones compartidas.';
  el('#my-position').textContent='—';
  el('#my-points').textContent='0';
  setScoreView(points,false);
  el('#leaderboard-body').innerHTML=`<tr><td>—</td><td>🔍 Ranking no conectado</td><td>—</td></tr>`;
  el('#player-total').textContent='Sin datos compartidos';
  el('#ranking-note').textContent='Modo práctica: para ver jugadores de verdad hay que publicar este proyecto junto a su servidor.';
  el('#player-form').querySelector('button').disabled=true;
  el('#player-form').querySelector('button').textContent='Disponible al publicar';
  el('#create-duel').disabled=true;
  setStatus('🟠 VISTA PREVIA LOCAL: partidas individuales disponibles. Ranking, QR entre celulares y premios requieren el servidor publicado.');
}
function renderRanking(d){
  el('#player-total').textContent=`${fmt(d.players)} con apodo · ${fmt(d.players_played)} jugaron · ${fmt(d.duels_completed)} duelos`;
  const top = d.top||[];
  el('#leaderboard-body').innerHTML=top.length?top.map(x=>`<tr class="${x.me?'my-rank':''}"><td>${x.rank===1?'🥇':x.rank===2?'🥈':x.rank===3?'🥉':('#'+x.rank)}</td><td>${esc(x.nickname)}${x.me?' <small>(tú)</small>':''}</td><td>${fmt(x.points)}</td></tr>`).join(''):`<tr><td colspan="3">Todavía no hay jugadores. ¡Sé el primero!</td></tr>`;
  el('#ranking-note').textContent='Clasificación del servidor · actualizada automáticamente cada pocos segundos · se muestran hasta 30 posiciones.';
  if(d.me)paintProfile(d.me);
}
async function refreshRanking(){if(!state.live)return;try{renderRanking(await api('/leaderboard'))}catch(e){setStatus('⚠️ Sin conexión con el ranking. Los puntos oficiales no se guardarán hasta reconectar.')}}
function localGrant(kind,key,answer,delta){
  const event=kind+':'+key;
  if(delta>0){if(state.practiceLedger[event]){notify('Esta actividad ya te dio puntos. Ahora es práctica.');return}
    state.practiceLedger[event]=1;persist();score(delta);
  }else if(delta<0){
    if(kind==='cw'){
      const k=`bad:${event}:${String(answer).toUpperCase()}`;
      if(state.practiceLedger[k])return;
      state.practiceLedger[k]=1;persist();
    }
    score(delta);
  }
  setScoreView(points,false);
}
window.gameAward=async function(kind,key,answer,delta){
  if(!state.live||!state.player){localGrant(kind,key,answer,delta);return}
  if(kind==='cw_bonus'||kind==='ws_bonus')return; // auto-assigned by server
  try{
    const data=await api('/award',{method:'POST',body:{kind,key,answer}});
    if(data.profile)paintProfile(data.profile);
    if(data.already)notify('¡Excelente! Ya ganaste puntos por esa respuesta anteriormente; repetir es solo entrenamiento.');
    else if(data.bonus)notify(`🏆 ¡Bono de nivel! +${data.bonus} puntos oficiales`);
    await Promise.allSettled([refreshRanking(),refreshRewards()]);
  }catch(e){notify('⚠️ Puntos no registrados: '+e.message);setStatus('⚠️ Sin conexión: los puntos para premios no se registraron.');}
};

function makeRewards(terms){
  state.allTiers=terms;
  el('#reward-grid').innerHTML=terms.map(t=>{
    const status=t.claim?(t.claim.redeemed?'<span class="reward-tag claimed">✅ Entregado</span>':`<div class="claim-code">Código privado: <strong>${esc(t.claim.code)}</strong></div>`):t.eligible?`<button class="btn btn-primary reward-claim" data-tier="${esc(t.id)}">Generar código de canje</button>`:`<small>${fmt(t.points)} puntos para desbloquear</small>`;
    return `<div class="reward-card ${t.eligible?'unlocked':''}"><span class="reward-icon">${esc(t.icon)}</span><b>${fmt(t.points)} puntos</b><span>${esc(t.label)}</span>${status}</div>`;
  }).join('');
  document.querySelectorAll('.reward-claim').forEach(b=>b.addEventListener('click',()=>requestClaim(b.dataset.tier)));
}
async function refreshRewards(){if(!state.live)return;try{const data=await api('/rewards');makeRewards(data.tiers)}catch{}}
async function requestClaim(id){
  if(!state.player)return notify('Elige un apodo antes de solicitar un premio.');
  try{const data=await api('/claims',{method:'POST',body:{tier_id:id}});
    el('#claim-message').textContent=`🎟️ Tu código de canje: ${data.code}. Muéstralo solamente a la persona encargada del stand; no lo compartas con otros jugadores.`;
    await refreshRewards();
  }catch(e){notify(e.message)}
}
async function createRoom(){
  if(!state.live)return notify('Publica el sitio con su servidor para conectar dos celulares.');
  if(!state.player){notify('Primero entra al ranking con tu apodo.');location.hash='#ranking';return}
  try{const r=await api('/duels',{method:'POST'});
    beginRoom(r.code,r.link,r.qr);
  }catch(e){notify(e.message)}
}
function beginRoom(code,link='',qr=''){
  stopPoll();state.room={code,link,qr};
  document.querySelectorAll('[data-mode]').forEach(b=>b.click&&b.dataset.mode==='duel'&&b.click());
  el('#duel-room').hidden=false;
  el('#duel-room').innerHTML=`<div class="duel-invite"><img id="duel-mini-qr" src="${esc(qr||'/api/qr/duel/'+code)}" alt="Mini QR para entrar a un duelo en otro celular" width="170" height="170"><div><b>📲 Invita a tu contrincante</b><p>La otra persona abre la cámara, escanea este mini QR y elige su apodo. Ambos contestarán las mismas 8 preguntas.</p><div class="mini-duel-code">Sala: ${esc(code)}</div><button type="button" class="mini-button" id="copy-duel">📋 Copiar enlace de invitación</button></div></div>`;
  const finalLink=link||`${location.origin}/?sala=${encodeURIComponent(code)}#juegos`;
  el('#copy-duel').addEventListener('click',()=>navigator.clipboard?.writeText(finalLink).then(()=>notify('Enlace copiado')).catch(()=>window.prompt('Copia este enlace:',finalLink))||window.prompt('Copia este enlace:',finalLink));
  refreshRoom();state.poll=setInterval(refreshRoom,1650);
  location.hash='#juegos';
}
function stopPoll(){if(state.poll)clearInterval(state.poll);state.poll=null}
function clearRoom(){stopPoll();state.room=null;el('#duel-room').hidden=true;el('#duel-room').innerHTML=''}
function renderRoom(room){
  if(!state.room||state.room.code!==room.code)return;
  el('#duel-p1').textContent=fmt(room.host.score);
  el('#duel-p2').textContent=fmt(room.guest?.score||0);
  el('#duel-panel .duel-score > div:first-child').firstChild.textContent=`🔵 ${room.host.nickname} `;
  el('#duel-panel .duel-score > div:last-child').firstChild.textContent=`🔴 ${room.guest?.nickname||'Esperando rival'} `;
  if(room.status==='waiting'){
    el('#duel-board').innerHTML=`<div class="duel-hold"><h4>⏳ Esperando al contrincante…</h4><p>Cuando escanee el QR comenzarán ocho rondas. Solo los primeros ocho aciertos de tu perfil pueden dar puntos oficiales, así no sirve repetir con el mismo amigo.</p></div>`;
    return;
  }
  if(room.status==='expired'){
    el('#duel-board').innerHTML='<h4>⌛ El mini QR venció. Crea otro para comenzar.</h4>';
    stopPoll();return;
  }
  if(room.status==='finished'){
    el('#duel-board').innerHTML=`<div class="duel-final"><span class="overline">DUELO COMPLETADO</span><h4>🏆 ${room.winner==='Empate'?'¡Empate!':`Ganó ${esc(room.winner)}`}</h4><p>${esc(room.host.nickname)}: ${fmt(room.host.score)} · ${esc(room.guest.nickname)}: ${fmt(room.guest.score)} puntos de duelo.</p><p>Podéis repetir para entrenar. Cada pregunta solo cuenta una vez para los puntos oficiales de premios.</p><button class="btn btn-primary" id="new-room">Crear nuevo duelo</button></div>`;
    el('#new-room').addEventListener('click',()=>{clearRoom();createRoom()});
    stopPoll();refreshRanking();refreshRewards();return;
  }
  const info=room.revealed
    ? `<div class="duel-feedback"><b>✅ Respuesta: ${esc(room.choices[room.correct_choice])}</b><p>${esc(room.explanation)}</p><small>La siguiente pregunta aparecerá automáticamente.</small></div>`
    : room.answered?`<p class="duel-hold-text">✅ Ya respondiste. ${room.opponent_answered?'El rival terminó; comprobando…':'Esperando la respuesta del otro detective…'}</p>`
    : `<p class="duel-hold-text">Selecciona la decisión más adecuada. Responder incorrectamente resta solo 5 puntos oficiales.</p>`;
  el('#duel-board').innerHTML=`<div class="duel-live-round"><div class="duel-round-info"><span>RONDA ${room.round}/8</span><span>⏱ ${room.seconds_left} s por pregunta</span></div><h4>${esc(room.question)}</h4><div class="duel-options">${room.choices.map((c,i)=>`<button type="button" data-live-choice="${i}" ${room.answered||room.revealed?'disabled':''}>${esc(c)}</button>`).join('')}</div>${info}</div>`;
  el('#duel-board').querySelectorAll('[data-live-choice]').forEach(b=>b.addEventListener('click',()=>submitRoomAnswer(Number(b.dataset.liveChoice))));
}
async function submitRoomAnswer(choice){
  if(!state.room||state.room.sending)return;
  state.room.sending=true;
  document.querySelectorAll('[data-live-choice]').forEach(b=>b.disabled=true);
  try{const r=await api(`/duels/${encodeURIComponent(state.room.code)}/answer`,{method:'POST',body:{choice}});
    if(!r.awarded)notify('Tu respuesta contó en el duelo, pero este turno ya no da puntos oficiales (límite anti-farmeo).');
    await refreshRoom();
  }catch(e){notify(e.message)}finally{if(state.room)state.room.sending=false}
}
let roomLoading=false;
async function refreshRoom(){
  if(!state.room||roomLoading)return;
  roomLoading=true;
  try{const data=await api('/duels/'+encodeURIComponent(state.room.code));renderRoom(data)}
  catch(e){setStatus('⚠️ No se pudo actualizar la sala. Revisa la conexión.');}
  finally{roomLoading=false}
}
async function autoJoin(){
  if(!state.live||!state.player)return;
  const code=(new URLSearchParams(location.search)).get('sala');
  if(!code||state.room?.code===code)return;
  try{await api('/duels/join',{method:'POST',body:{code}});beginRoom(code)}
  catch(e){notify('No se pudo entrar al duelo: '+e.message);}
}
async function saveNickname(event){
  event.preventDefault();if(!state.live)return;
  const nickname=el('#player-nickname').value.trim();
  try{const firstOfficialProfile=!state.player;paintProfile(await api('/profile',{method:'POST',body:{nickname}}));
    // Do not accidentally strand a new entrant with all puzzles already marked
    // as practice-solved. Practice scores never move to official standings.
    if(firstOfficialProfile){
      if(typeof cwSaved!=='undefined'){cwSaved={};try{localStorage.setItem('micro-v3-cw','{}')}catch{};if(typeof cwInitLevel==='function')cwInitLevel(0)}
      if(typeof ws!=='undefined'){ws.found=new Set();ws.round=0;ws.started=false;try{localStorage.setItem('micro-v3-ws','[]')}catch{};if(typeof wsInit==='function')wsInit()}
      try{localStorage.removeItem('micro-v3-ws-bonus')}catch{}
      if(typeof wsBonusGranted!=='undefined')wsBonusGranted=false;
    }
    setStatus('🟢 PUNTOS OFICIALES ACTIVOS · No compartas tu dispositivo ni borres sus datos hasta retirar tus premios.');
    await Promise.allSettled([refreshRanking(),refreshRewards()]);
    notify('¡Ya estás en la liga! Tus puntos oficiales empiezan desde cero y se guardan en el servidor.');
    await autoJoin();
  }catch(e){notify('No se pudo activar tu perfil: '+e.message)}
}
async function init(){
  el('#player-form').addEventListener('submit',saveNickname);
  el('#create-duel').addEventListener('click',createRoom);
  el('#practice-duel').addEventListener('click',()=>{clearRoom();if(typeof duelRestart==='function')duelRestart();notify('Modo entrenamiento de dos personas en un mismo dispositivo. Sin puntos oficiales.')});
  try{
    // A local HTML file cannot create genuine shared players. Do not fake live features.
    if(location.protocol==='file:')throw new Error('local');
    const h=await api('/health');
    if(!h.live)throw new Error('not live');
    state.live=true;
    const p=await api('/me');
    if(!p.guest){paintProfile(p);setStatus('🟢 CONECTADO · Ranking y premios activos · perfil sin registro.');}
    else {setStatus('🟢 CONECTADO · Elige tu apodo para empezar a sumar puntos oficiales.');if(new URLSearchParams(location.search).get('sala')){document.querySelector('[data-mode=duel]').click();el('#duel-board').innerHTML='<div class="duel-hold"><h4>📲 ¡Escaneaste una invitación!</h4><p>Primero elige un apodo en la sección Liga; después entrarás automáticamente al duelo.</p></div>';location.hash='#ranking';}}
    el('#event-qr').hidden=false;
    el('#event-qr-image').src='/api/qr/site';
    el('#event-qr-link').href='/api/qr/site';
    el('#create-duel').disabled=false;
    await Promise.allSettled([refreshRanking(),refreshRewards()]);
    if(state.player)await autoJoin();
    setInterval(()=>{refreshRanking();refreshRewards()},9500);
  }catch(e){state.live=false;paintLocal();}
}
window.microLeague={hasLiveRoom:()=>!!state.room,leaveRoom:clearRoom,
 practiceDuelPoint:(n)=>{if(!state.live){localGrant('duel-practice','local:'+String(Date.now()),'',n)}else notify('Duelo de prueba: no añade puntos oficiales.')}};
init();
})();