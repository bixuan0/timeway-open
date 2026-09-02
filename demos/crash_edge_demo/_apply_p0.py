# -*- coding: utf-8 -*-
import io, sys
p = "index.html"
with io.open(p, "r", encoding="utf-8") as f:
    txt = f.read()

reps = [
 # 0: HUD money pulse CSS
 ("#hudMoney{top:12px;left:12px;color:#ffd700;font-weight:700;font-size:18px}",
  "#hudMoney{top:12px;left:12px;color:#ffd700;font-weight:700;font-size:18px;display:inline-block;transform-origin:left center}\n@keyframes pulseMoney{0%{transform:scale(1)}40%{transform:scale(1.4);filter:brightness(1.6)}100%{transform:scale(1)}}\n.pulse{animation:pulseMoney 0.3s ease-out}"),
 # 1: anxiety jitter up
 ("anxiety:{crash:25,nearMiss:8,redLight:5,timeout:30,timePressure:0.015,deliver:-15,idle:-0.04,max:100,high:60,jitter:0.12},",
  "anxiety:{crash:25,nearMiss:8,redLight:5,timeout:30,timePressure:0.015,deliver:-15,idle:-0.04,max:100,high:60,jitter:0.2},"),
 # 2: G def order -> orders
 ("player:new Player(),cars:[],order:null,money:C.gameOver.startMoney,",
  "player:new Player(),cars:[],orders:[],orderSeq:0,money:C.gameOver.startMoney,"),
 # 3: maxOrders method before spawnOrder
 ("spawnOrder(){",
  "maxOrders(){return Math.min(3,2+Math.floor(this.difficulty*2));},\nspawnOrder(){"),
 # 4: spawnOrder body
 ("spawnOrder(){\nconst pickup=randDoor();\nlet delivery;do{delivery=randDoor()}while(Math.hypot(pickup.x-delivery.x,pickup.y-delivery.y)<150);\nthis.order={state:'pickup',pickup,delivery,timer:C.order.timer,maxTimer:C.order.timer};\n},",
  "spawnOrder(){\nif(this.orders.length>=this.maxOrders())return;\nconst pickup=randDoor();\nlet delivery;do{delivery=randDoor()}while(Math.hypot(pickup.x-delivery.x,pickup.y-delivery.y)<150);\nconst timer=Math.max(30000,C.order.timer-this.difficulty*15000);\nthis.orders.push({id:++this.orderSeq,state:'pickup',pickup,delivery,timer,maxTimer:timer});\n},"),
 # 5: start()
 ("particles=[];floatTexts=[];this.order=null;this.spawnOrder();",
  "particles=[];floatTexts=[];this.orders=[];this.orderSeq=0;this.spawnOrder();this.spawnOrder();"),
 # 6: anxiety time pressure
 ("// Anxiety time pressure\nif(this.order&&this.order.timer<10000){\nthis.anxiety=Math.min(C.anxiety.max,this.anxiety+C.anxiety.timePressure);\nthis.warnCooldown-=dt;\nif(this.warnCooldown<=0){SND.warn();this.warnCooldown=1000;}\n}",
  "// Anxiety time pressure (any urgent order)\nlet _urgent=false;\nfor(const o of this.orders)if(o.timer<10000)_urgent=true;\nif(_urgent){\nthis.anxiety=Math.min(C.anxiety.max,this.anxiety+C.anxiety.timePressure);\nthis.warnCooldown-=dt;\nif(this.warnCooldown<=0){SND.warn();this.warnCooldown=1000;}\n}"),
 # 7: order loop
 ("// Order\nif(this.order){\nthis.order.timer-=dt;\nif(this.order.timer<=0){this.orderTimeout();}\nelse{this.checkOrderProgress();}\n}",
  "// Orders (parallel)\nfor(let i=this.orders.length-1;i>=0;i--){\nconst o=this.orders[i];\no.timer-=dt;\nif(o.timer<=0){this.orderTimeout(o);}\nelse{this.checkOrderProgress(o);}\n}\nif(this.orders.length<this.maxOrders())this.spawnOrder();"),
 # 8: Player.update anxiety felt
 ("// Anxiety jitter\nif(anxiety>40){const j=(anxiety-40)/60*C.anxiety.jitter;ax+=(Math.random()-0.5)*j*2;ay+=(Math.random()-0.5)*j*2;}\n// Anxiety panic steering\nif(anxiety>80&&Math.random()<0.005){ax+=(Math.random()-0.5)*3;ay+=(Math.random()-0.5)*3;}",
  "// Anxiety jitter (felt steering distortion)\nif(anxiety>30){const j=(anxiety-30)/70*C.anxiety.jitter*2.2;ax+=(Math.random()-0.5)*j*2;ay+=(Math.random()-0.5)*j*2;}\n// Anxiety steering drift (harder to hold a line)\nif(anxiety>60){const drift=(anxiety-60)/40*0.5;ax+=(Math.random()-0.5)*drift*2;ay+=(Math.random()-0.5)*drift*2;}\n// Anxiety panic jerk (sudden loss of control)\nif(anxiety>80&&Math.random()<0.02){ax+=(Math.random()-0.5)*4;ay+=(Math.random()-0.5)*4;}"),
 # 9: Player.render glow
 ("ctx.rotate(this.angle);\n// Shadow\nctx.fillStyle='rgba(0,0,0,0.4)';",
  "ctx.rotate(this.angle);\n// Glow ring (player always visible)\nconst _gnow=Date.now();\nconst _gg=ctx.createRadialGradient(0,0,6,0,0,32);\n_gg.addColorStop(0,'rgba(0,229,255,0.40)');_gg.addColorStop(1,'rgba(0,229,255,0)');\nctx.fillStyle=_gg;ctx.beginPath();ctx.arc(0,0,32,0,Math.PI*2);ctx.fill();\nctx.strokeStyle='rgba(0,229,255,0.9)';ctx.lineWidth=2.5;\nctx.beginPath();ctx.arc(0,0,21+Math.sin(_gnow/250)*2,0,Math.PI*2);ctx.stroke();\n// Shadow\nctx.fillStyle='rgba(0,0,0,0.4)';"),
 # 10: checkOrderProgress
 ("checkOrderProgress(){\nif(!this.order)return;\nconst p=this.player;\nif(this.order.state==='pickup'){\nconst d=Math.hypot(p.x-this.order.pickup.x,p.y-this.order.pickup.y);\nif(d<C.order.pickupRadius){\nthis.order.state='delivery';SND.pickup();\nburst(this.order.pickup.x,this.order.pickup.y,12,['#ffd700','#fff5a0','#ffaa00'],4,4,30);\nfloatText(this.order.pickup.x,this.order.pickup.y-15,'取餐成功','#ffd700',14);\n}\n}else if(this.order.state==='delivery'){\nconst d=Math.hypot(p.x-this.order.delivery.x,p.y-this.order.delivery.y);\nif(d<C.order.pickupRadius){this.deliverOrder();}\n}\n},",
  "checkOrderProgress(o){\nif(!o)return;\nconst p=this.player;\nif(o.state==='pickup'){\nconst d=Math.hypot(p.x-o.pickup.x,p.y-o.pickup.y);\nif(d<C.order.pickupRadius){\no.state='delivery';SND.pickup();\nburst(o.pickup.x,o.pickup.y,12,['#ffd700','#fff5a0','#ffaa00'],4,4,30);\nfloatText(o.pickup.x,o.pickup.y-15,'取餐成功','#ffd700',14);\n}\n}else if(o.state==='delivery'){\nconst d=Math.hypot(p.x-o.delivery.x,p.y-o.delivery.y);\nif(d<C.order.pickupRadius){this.deliverOrder(o);}\n}\n},"),
 # 11: deliverOrder
 ("deliverOrder(){\nthis.deliveries++;this.combo++;\nconst bonus=Math.min(this.combo-1,4);\nconst reward=C.order.reward+bonus*5;\nthis.money+=reward;\nthis.anxiety=Math.max(0,this.anxiety+C.anxiety.deliver);\nif(this.order.timer>this.order.maxTimer*0.6){this.anxiety=Math.max(0,this.anxiety-5);}\nburst(this.order.delivery.x,this.order.delivery.y,25,['#00ff88','#88ffaa','#ffffff','#ffd700'],6,5,50);\nfloatText(this.order.delivery.x,this.order.delivery.y-15,'+¥'+reward,'#00ff88',20);\nif(this.combo>=2)floatText(this.order.delivery.x,this.order.delivery.y-35,'连击 x'+this.combo+'!','#ff9500',14);\nSND.deliver();\n// Shorter timer with difficulty\nconst newTimer=Math.max(30000,C.order.timer-this.difficulty*15000);\nthis.order=null;\nsetTimeout(()=>{if(this.state==='play'){this.order={state:'pickup',pickup:randDoor(),delivery:(()=>{let d;do{d=randDoor()}while(Math.hypot(this.player.x-d.x,this.player.y-d.y)<120);return d})(),timer:newTimer,maxTimer:newTimer}}},800);\n},",
  "deliverOrder(o){\nthis.deliveries++;this.combo++;\nconst bonus=Math.min(this.combo-1,4);\nconst reward=C.order.reward+bonus*5;\nthis.money+=reward;\nthis.anxiety=Math.max(0,this.anxiety+C.anxiety.deliver);\nif(o.timer>o.maxTimer*0.6){this.anxiety=Math.max(0,this.anxiety-5);}\nburst(o.delivery.x,o.delivery.y,25,['#00ff88','#88ffaa','#ffffff','#ffd700'],6,5,50);\nfloatText(o.delivery.x,o.delivery.y-15,'+¥'+reward,'#00ff88',20);\nif(this.combo>=2)floatText(o.delivery.x,o.delivery.y-35,'连击 x'+this.combo+'!','#ff9500',14);\nSND.deliver();\n// Remove delivered order & maintain order cap\nthis.orders=this.orders.filter(x=>x!==o);\nif(this.orders.length<this.maxOrders())this.spawnOrder();\n},"),
 # 12: orderTimeout
 ("orderTimeout(){\nthis.money-=C.order.penaltyTimeout;\nthis.anxiety=Math.min(C.anxiety.max,this.anxiety+C.anxiety.timeout);\nthis.combo=0;\nfloatText(this.player.x,this.player.y-25,'超时! -¥'+C.order.penaltyTimeout,'#ff4444',16);\nSND.warn();\nconst newTimer=Math.max(30000,C.order.timer-this.difficulty*15000);\nthis.order={state:'pickup',pickup:randDoor(),delivery:(()=>{let d;do{d=randDoor()}while(Math.hypot(this.player.x-d.x,this.player.y-d.y)<120);return d})(),timer:newTimer,maxTimer:newTimer};\n},",
  "orderTimeout(o){\nthis.money-=C.order.penaltyTimeout;\nthis.anxiety=Math.min(C.anxiety.max,this.anxiety+C.anxiety.timeout);\nthis.combo=0;\nfloatText(this.player.x,this.player.y-25,'超时! -¥'+C.order.penaltyTimeout,'#ff4444',16);\nSND.warn();\nthis.orders=this.orders.filter(x=>x!==o);\nif(this.orders.length<this.maxOrders())this.spawnOrder();\n},"),
 # 13: door glow
 ("// Door glow for active order\nif(this.order){\nif(this.order.state==='pickup'&&this.order.pickup.building===b){\nconst dp=this.order.pickup;\nconst g=ctx.createRadialGradient(dp.x,dp.y,0,dp.x,dp.y,40);g.addColorStop(0,'rgba(255,215,0,0.3)');g.addColorStop(1,'rgba(255,215,0,0)');ctx.fillStyle=g;ctx.beginPath();ctx.arc(dp.x,dp.y,40,0,Math.PI*2);ctx.fill();}\nif(this.order.state==='delivery'&&this.order.delivery.building===b){\nconst dp=this.order.delivery;\nconst g=ctx.createRadialGradient(dp.x,dp.y,0,dp.x,dp.y,40);g.addColorStop(0,'rgba(0,255,136,0.3)');g.addColorStop(1,'rgba(0,255,136,0)');ctx.fillStyle=g;ctx.beginPath();ctx.arc(dp.x,dp.y,40,0,Math.PI*2);ctx.fill();}\n}",
  "// Door glow for active orders\nfor(const o of this.orders){\nif(o.state==='pickup'&&o.pickup.building===b){\nconst dp=o.pickup;\nconst g=ctx.createRadialGradient(dp.x,dp.y,0,dp.x,dp.y,40);g.addColorStop(0,'rgba(255,215,0,0.3)');g.addColorStop(1,'rgba(255,215,0,0)');ctx.fillStyle=g;ctx.beginPath();ctx.arc(dp.x,dp.y,40,0,Math.PI*2);ctx.fill();}\nif(o.state==='delivery'&&o.delivery.building===b){\nconst dp=o.delivery;\nconst g=ctx.createRadialGradient(dp.x,dp.y,0,dp.x,dp.y,40);g.addColorStop(0,'rgba(0,255,136,0.3)');g.addColorStop(1,'rgba(0,255,136,0)');ctx.fillStyle=g;ctx.beginPath();ctx.arc(dp.x,dp.y,40,0,Math.PI*2);ctx.fill();}\n}"),
 # 14: order markers opening
 ("// Order markers\nif(this.order){",
  "// Order markers\nfor(const o of this.orders){"),
 # 15: markers pickup
 ("if(this.order.state==='pickup'){\nconst p=this.order.pickup;",
  "if(o.state==='pickup'){\nconst p=o.pickup;"),
 # 16: markers delivery
 ("const p=this.order.delivery;\nctx.save();ctx.translate(p.x,p.y);",
  "const p=o.delivery;\nctx.save();ctx.translate(p.x,p.y);"),
 # 17: updateHUD order
 ("const od=document.getElementById('hudOrder');\nif(G.order){\nconst lb=od.querySelector('.label');\nconst tm=od.querySelector('.timer');\nconst tf=od.querySelector('.timer-fill');\nif(G.order.state==='pickup'){lb.textContent='🟡 取餐中';lb.style.color='#ffd700';}\nelse{lb.textContent='🟢 送餐中';lb.style.color='#00ff88';}\nconst sec=Math.ceil(G.order.timer/1000);\ntm.textContent=sec+'″';\ntm.style.color=sec<10?'#ff4444':'#e0e8ff';\ntf.style.width=Math.max(0,G.order.timer/G.order.maxTimer*100)+'%';\ntf.style.background=sec<10?'#ff4444':'#00e5ff';\n}else{od.querySelector('.label').textContent='等待接单...';od.querySelector('.timer').textContent='';od.querySelector('.timer-fill').style.width='0%';}",
  "const od=document.getElementById('hudOrder');\nif(G.orders.length){\nlet best=G.orders[0];for(const o of G.orders)if(o.timer<best.timer)best=o;\nconst lb=od.querySelector('.label');\nconst tm=od.querySelector('.timer');\nconst tf=od.querySelector('.timer-fill');\nlb.textContent='📦 '+G.orders.length+'单 · '+(best.state==='pickup'?'取餐':'送餐');\nlb.style.color=best.state==='pickup'?'#ffd700':'#00ff88';\nconst sec=Math.ceil(best.timer/1000);\ntm.textContent=sec+'″';\ntm.style.color=sec<10?'#ff4444':'#e0e8ff';\ntf.style.width=Math.max(0,best.timer/best.maxTimer*100)+'%';\ntf.style.background=sec<10?'#ff4444':'#00e5ff';\n}else{od.querySelector('.label').textContent='等待接单...';od.querySelector('.timer').textContent='';od.querySelector('.timer-fill').style.width='0%';}"),
 # 18: updateHUD money pulse
 ("const m=document.getElementById('hudMoney');\nm.textContent='¥'+G.money;\nm.style.color=G.money<20?'#ff4444':'#ffd700';",
  "const m=document.getElementById('hudMoney');\nm.textContent='¥'+G.money;\nm.style.color=G.money<20?'#ff4444':'#ffd700';\nif(G._lastMoneyShown!==undefined&&G._lastMoneyShown!==G.money){m.classList.remove('pulse');void m.offsetWidth;m.classList.add('pulse');}\nG._lastMoneyShown=G.money;"),
 # 19: windows 0.7
 ("if(lit){ctx.fillStyle='rgba(255,215,80,0.7)';ctx.fillRect(wx,wy,winSz,winSz);",
  "if(lit){ctx.fillStyle='rgba(255,215,80,0.45)';ctx.fillRect(wx,wy,winSz,winSz);"),
 # 20: windows 0.06
 ("ctx.fillStyle='rgba(255,215,80,0.06)';ctx.fillRect(wx-2,wy-2,winSz+4,winSz+4);}",
  "ctx.fillStyle='rgba(255,215,80,0.03)';ctx.fillRect(wx-2,wy-2,winSz+4,winSz+4);}"),
]

miss = []
applied = 0
for i,(old,new) in enumerate(reps):
    cnt = txt.count(old)
    if cnt == 0:
        miss.append(i)
        # print surrounding diagnostics
        print("MISS edit", i, "--- first 60 chars of old:")
        print(repr(old[:60]))
    elif cnt > 1:
        print("WARN edit", i, "matches", cnt, "times (applying first)")
        txt = txt.replace(old, new, 1)
        applied += 1
    else:
        txt = txt.replace(old, new)
        applied += 1

print("applied:", applied, "missed:", miss)
with io.open(p, "w", encoding="utf-8") as f:
    f.write(txt)
print("written")
