"use strict";

// Layout, time spacing, focus targets, and linked selection follow the reference dashboard.
window.ExplorerCharts = (() => {
  const ns = "http://www.w3.org/2000/svg";
  const compact = new Intl.NumberFormat(undefined, {notation:"compact",maximumFractionDigits:1});
  const precise = new Intl.NumberFormat(undefined, {maximumFractionDigits:4});
  function svg(tag, attributes = {}, label) {
    const element = document.createElementNS(ns, tag);
    for (const [key,value] of Object.entries(attributes)) element.setAttribute(key,value);
    if (label !== undefined) element.textContent = label;
    return element;
  }
  function periodLabel(item, period) {
    const [year,month] = item.start.split("-");
    if (period === "year") return year;
    if (period === "quarter") return year+"-Q"+Math.ceil(Number(month)/3);
    if (period === "month") return year+"-"+month;
    return item.start;
  }
  function time(target, items, options) {
    target.replaceChildren();
    const values = items.filter(item => item[options.metric] !== null && Number.isFinite(item[options.metric]));
    if (!values.length) {
      const empty = document.createElement("p");
      empty.className = "note";
      empty.textContent = "No dated values match this selection.";
      target.append(empty);
      return;
    }
    const width=760,height=300,left=68,right=20,top=20,bottom=43;
    const baseline=height-bottom;
    const low=Math.min(0,...values.map(item=>item[options.metric]));
    const high=Math.max(0,...values.map(item=>item[options.metric]));
    const span=high-low || 1;
    const y=value=>baseline-(value-low)/span*(baseline-top);
    const start=Date.parse(values[0].start),end=Date.parse(values.at(-1).start);
    const x=item=>end===start?(left+width-right)/2:left+(Date.parse(item.start)-start)/(end-start)*(width-left-right);
    const root=svg("svg",{viewBox:`0 0 ${width} ${height}`,role:"group","aria-label":options.label});
    const fillId="trend-fill-"+Math.random().toString(36).slice(2);
    const defs=svg("defs");
    const gradient=svg("linearGradient",{id:fillId,x1:0,x2:0,y1:0,y2:1});
    gradient.append(svg("stop",{offset:"0%","stop-color":"var(--accent)","stop-opacity":".25"}),svg("stop",{offset:"100%","stop-color":"var(--accent)","stop-opacity":".025"}));
    defs.append(gradient); root.append(defs);
    for(let i=0;i<=4;i++){
      const value=low+span*i/4;
      root.append(svg("line",{x1:left,x2:width-right,y1:y(value),y2:y(value),stroke:"var(--border)"}));
      root.append(svg("text",{x:left-9,y:y(value)+4,"text-anchor":"end"},compact.format(value)));
    }
    const zero=y(0);
    if(options.type!=="bars"){
      const periodIndex=item=>{
        const [year,month]=item.start.split("-").map(Number);
        if(options.period==="year")return year;
        if(options.period==="quarter")return year*4+Math.floor((month-1)/3);
        if(options.period==="month")return year*12+month;
        return Date.parse(item.start)/(options.period==="week"?604800000:86400000);
      };
      const runs=[];
      for(const item of values){
        const previous=runs.at(-1)?.at(-1);
        if(!previous||Math.abs(periodIndex(item)-periodIndex(previous)-1)>.001)runs.push([]);
        runs.at(-1).push(item);
      }
      for(const run of runs){
        if(run.length<2)continue;
        const coordinates=run.map(item=>x(item)+","+y(item[options.metric])).join(" ");
        if(options.type==="area")root.append(svg("polygon",{
          points:x(run[0])+","+zero+" "+coordinates+" "+x(run.at(-1))+","+zero,fill:`url(#${fillId})`
        }));
        root.append(svg("polyline",{points:coordinates,fill:"none",stroke:"var(--accent)","stroke-width":2.5,"stroke-linejoin":"round"}));
      }
    }
    const step=Math.max(1,Math.ceil(values.length/6));
    const marks=[];
    const tooltip=document.createElement("div");
    tooltip.className="chart-tooltip"; tooltip.hidden=true;
    values.forEach((item,index)=>{
      const px=x(item),py=y(item[options.metric]);
      const hint=periodLabel(item,options.period)+" · "+options.metricLabel+" "+precise.format(item[options.metric])+" · "+precise.format(item.n)+" records";
      const group=svg("g",{class:"trend-hit",role:"button",tabindex:index===0||values.length<=120?0:-1,"aria-label":hint});
      group.append(svg("title",{},hint));
      const spacing=Math.max(2,(width-left-right)/values.length);
      if(options.type==="bars") group.append(svg("rect",{x:px-Math.min(32,spacing*.72)/2,y:Math.min(py,zero),width:Math.min(32,spacing*.72),height:Math.max(1,Math.abs(py-zero)),rx:2,fill:"var(--accent)",opacity:.82}));
      else group.append(svg("circle",{cx:px,cy:py,r:values.length>150?1.5:3.5,fill:"var(--accent)"}));
      group.append(svg("rect",{x:px-Math.max(2,Math.min(15,spacing/2)),y:top,width:Math.max(4,Math.min(30,spacing)),height:baseline-top,fill:"transparent"}));
      group.addEventListener("click",()=>options.onSelect(item));
      group.addEventListener("keydown",event=>{
        if(event.key==="Enter"||event.key===" "){event.preventDefault();options.onSelect(item);}
        if(event.key==="ArrowLeft"||event.key==="ArrowRight"){
          event.preventDefault();
          const next=marks[Math.max(0,Math.min(values.length-1,index+(event.key==="ArrowRight"?1:-1)))];
          group.setAttribute("tabindex",-1);next.setAttribute("tabindex",0);next.focus();
        }
      });
      const show=()=>{
        tooltip.textContent=hint; tooltip.hidden=false;
        tooltip.style.left=Math.max(0,Math.min(70,px/width*100))+"%";
      };
      group.addEventListener("mouseenter",show); group.addEventListener("focus",show);
      group.addEventListener("mouseleave",()=>{tooltip.hidden=true;});group.addEventListener("blur",()=>{tooltip.hidden=true;});
      marks.push(group);root.append(group);
      if(index%step===0||index===values.length-1){
        root.append(svg("text",{x:px,y:baseline+23,"text-anchor":index===0?"start":index===values.length-1?"end":"middle"},periodLabel(item,options.period)));
      }
    });
    target.append(root,tooltip);
  }
  return {time,periodLabel};
})();
