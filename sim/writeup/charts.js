// Charts for the write-up, drawn from data.json (sim/analyze.py).
// Numbers in the text are filled from the same data, so text and charts agree.

const W = 700;
const ROW = 26;
const LEFT = 170;
const RIGHT = 60;
const PRODUCTION = "1000:10000";
const FEE_NAMES = {
  "1000:10000": "1 sat + 1% (now)",
  "1000:5000": "1 sat + 0.5%",
  "0:15000": "1.5%",
  "5000:5000": "5 sats + 0.5%",
};
const FEE_ORDER = ["0:15000", "1000:10000", "5000:5000", "1000:5000"];
const PARAM_NAMES = {
  tail_share: "Share of expensive items",
  routing_base_msat: "Routing base cost",
  routing_ppm: "Routing cost per amount",
  payout_fail_base: "Payouts that fail",
  payout_stuck: "Payouts that get stuck",
  payout_lost_answer: "Payouts with a lost answer",
  sales_per_hour: "Sales per hour",
  node_outages_per_day: "Node outages",
  reserve_rejected: "Items the game refuses",
  cancel_error: "Errors on refunding",
  weekend_factor: "Busier weekends",
  commit_rejected: "Hand-overs the game refuses",
  resolve_fail: "Payout status lookups that fail",
  amount_sigma: "Spread of prices",
  amount_median_sats: "Typical price",
  payout_stuck_median_s: "How long stuck payouts last",
};
const OUTCOME_NAMES = {
  fronted_peak_sats: "Peak money fronted",
  refunds_per_1000_paid: "Refunds",
  stuck_minutes: "Time with stuck sales",
  profit_share_of_volume: "Profit as a share of volume",
};

const sats = (x) => `${d3.format(",")(Math.round(x))} sats`;
const ksats = (x) => (x >= 1e6 ? `${d3.format(".2~f")(x / 1e6)}M` : `${d3.format("~s")(x)}`);
const ratio = (x) => `${x.toFixed(2)}×`;
const per1000 = (x) => x.toFixed(1);
const pct = (x, digits = 0) => `${(100 * x).toFixed(digits)}%`;
const duration = (s) => (s < 120 ? `${Math.round(s)} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${d3.format(".1~f")(s / 3600)} h`);
const signed = (x) => x.toFixed(2).replace("-", "−");

// ── Tooltip and table ──────────────────────────────────────────────────

function tooltip(container) {
  const tip = d3.select(container).append("div").attr("class", "tooltip").style("display", "none");
  return {
    show(event, lines) {
      // Built with textContent only.
      tip.selectAll("*").remove();
      lines.forEach((text, i) => {
        if (i) tip.append("br");
        tip.append(i === 0 ? "strong" : "span").text(text);
      });
      const box = container.getBoundingClientRect();
      tip.style("display", "block");
      const w = tip.node().offsetWidth;
      const x = event.clientX - box.left;
      tip.style("left", `${Math.min(Math.max(0, x - w / 2), box.width - w)}px`).style("top", `${event.clientY - box.top - 70}px`);
    },
    hide() {
      tip.style("display", "none");
    },
  };
}

function table(el, headers, rows) {
  const details = d3.select(el.closest("figure")).append("details");
  details.append("summary").text("data table");
  const t = details.append("table");
  t.append("thead").append("tr").selectAll("th").data(headers).join("th").text((d) => d);
  t.append("tbody").selectAll("tr").data(rows).join("tr").selectAll("td").data((r) => r).join("td").text((d) => d);
}

function svgFor(el, height, label) {
  return d3.select(el).append("svg").attr("viewBox", `0 0 ${W} ${height}`).attr("role", "img").attr("aria-label", label);
}

/** Horizontal bars from a zero line. rows: [{label, value, text, tip}]. Returns the height used. */
function barRows(svg, tip, top, rows, x, { title, ticks, axisFormat }) {
  let y = top;
  if (title) {
    svg.append("text").attr("class", "panel-title").attr("x", 0).attr("y", y + 12).text(title);
    y += 24;
  }
  const bodyTop = y;
  const bodyBottom = y + rows.length * ROW;
  svg.append("g").attr("class", "grid").selectAll("line").data(ticks).join("line")
    .attr("x1", x).attr("x2", x).attr("y1", bodyTop).attr("y2", bodyBottom);
  svg.append("line").attr("class", "ref").attr("x1", x(0)).attr("x2", x(0)).attr("y1", bodyTop).attr("y2", bodyBottom);
  if (axisFormat) {
    svg.append("g").attr("class", "axis").attr("transform", `translate(0,${bodyBottom})`)
      .call(d3.axisBottom(x).tickValues(ticks).tickFormat(axisFormat).tickSizeOuter(0));
  }
  rows.forEach((r, i) => {
    const cy = bodyTop + i * ROW + ROW / 2;
    svg.append("text").attr("class", "row-label").attr("x", 0).attr("y", cy + 4).text(r.label);
    const x0 = x(0);
    const x1 = x(r.value);
    svg.append("rect").attr("class", "bar").attr("x", Math.min(x0, x1)).attr("y", cy - 6)
      .attr("width", Math.max(1, Math.abs(x1 - x0))).attr("height", 12).attr("rx", 2);
    svg.append("text").attr("class", "value-label").attr("x", r.value < 0 ? x1 - 6 : x1 + 6).attr("y", cy + 4)
      .attr("text-anchor", r.value < 0 ? "end" : "start").text(r.text);
    svg.append("rect").attr("class", "hit").attr("x", 0).attr("y", cy - ROW / 2).attr("width", W).attr("height", ROW)
      .on("mousemove", (event) => tip.show(event, r.tip)).on("mouseleave", () => tip.hide());
  });
  return bodyBottom - top + (axisFormat ? 34 : 8);
}

/** Rows with a 90% range and a median dot. rows: [{label, lo, mid, hi, tip}]. Returns the height used. */
function rangeRows(svg, tip, top, rows, x, { title, ticks, axisFormat, ref }) {
  svg.append("text").attr("class", "panel-title").attr("x", 0).attr("y", top + 12).text(title);
  const bodyTop = top + 24;
  const bodyBottom = bodyTop + rows.length * ROW;
  svg.append("g").attr("class", "grid").selectAll("line").data(ticks).join("line")
    .attr("x1", x).attr("x2", x).attr("y1", bodyTop).attr("y2", bodyBottom);
  if (ref !== undefined) {
    svg.append("line").attr("class", "ref").attr("x1", x(ref)).attr("x2", x(ref)).attr("y1", bodyTop - 4).attr("y2", bodyBottom);
  }
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${bodyBottom})`)
    .call(d3.axisBottom(x).tickValues(ticks).tickFormat(axisFormat).tickSizeOuter(0));
  rows.forEach((r, i) => {
    const cy = bodyTop + i * ROW + ROW / 2;
    svg.append("text").attr("class", "row-label").attr("x", 0).attr("y", cy + 4).text(r.label);
    const g = svg.append("g").attr("class", "mark");
    g.append("line").attr("x1", x(r.lo)).attr("x2", x(r.hi)).attr("y1", cy).attr("y2", cy);
    g.append("circle").attr("cx", x(r.mid)).attr("cy", cy).attr("r", 4.5);
    svg.append("text").attr("class", "value-label").attr("x", x(r.hi) + 6).attr("y", cy + 4).text(r.text);
    svg.append("rect").attr("class", "hit").attr("x", 0).attr("y", cy - ROW / 2).attr("width", W).attr("height", ROW)
      .on("mousemove", (event) => tip.show(event, r.tip)).on("mouseleave", () => tip.hide());
  });
  return bodyBottom + 34 - top;
}

// ── Chart: peak fronted against the largest sale ───────────────────────

function frontedChart(el, points) {
  const H = 340;
  const M = { top: 10, right: 20, bottom: 44, left: 64 };
  const all = points.flatMap((p) => [p.peak, p.largest]);
  const domain = [d3.min(all) / 1.3, d3.max(all) * 1.3];
  const x = d3.scaleLog().domain(domain).range([M.left, W - M.right]);
  const y = d3.scaleLog().domain(domain).range([H - M.bottom, M.top]);
  const ticks = [1e4, 2e4, 5e4, 1e5, 2e5, 5e5, 1e6, 2e6, 5e6].filter((t) => t >= domain[0] && t <= domain[1]);
  const svg = svgFor(el, H, "Peak money fronted in each simulated week against that week's largest sale; the points lie on or just above the diagonal");
  const tip = tooltip(el);

  svg.append("g").attr("class", "grid").selectAll("line").data(ticks).join("line")
    .attr("x1", M.left).attr("x2", W - M.right).attr("y1", y).attr("y2", y);
  svg.append("line").attr("class", "ref").attr("x1", x(domain[0])).attr("y1", y(domain[0])).attr("x2", x(domain[1])).attr("y2", y(domain[1]));
  svg.append("text").attr("class", "ref-label").attr("x", x(1e5)).attr("y", y(1e5) + 18).attr("text-anchor", "start")
    .text("peak = largest sale");
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - M.bottom})`)
    .call(d3.axisBottom(x).tickValues(ticks).tickFormat(ksats).tickSizeOuter(0));
  svg.append("g").attr("class", "axis").attr("transform", `translate(${M.left},0)`)
    .call(d3.axisLeft(y).tickValues(ticks).tickFormat(ksats).tickSizeOuter(0));
  svg.append("text").attr("class", "axis-label").attr("x", W - M.right).attr("y", H - 6).attr("text-anchor", "end").text("largest sale of the week, sats");
  svg.append("text").attr("class", "axis-label").attr("x", 0).attr("y", 12).text("peak fronted, sats");

  for (const p of points) {
    svg.append("circle").attr("class", "dot").attr("cx", x(p.largest)).attr("cy", y(p.peak)).attr("r", 4.5);
    svg.append("circle").attr("class", "hit").attr("cx", x(p.largest)).attr("cy", y(p.peak)).attr("r", 10)
      .on("mousemove", (event) => tip.show(event, [`World ${p.seed}`, `peak fronted ${sats(p.peak)}`, `largest sale ${sats(p.largest)}`]))
      .on("mouseleave", () => tip.hide());
  }
  table(el, ["world", "largest sale (sats)", "peak fronted (sats)", "peak / largest"],
    points.map((p) => [p.seed, d3.format(",")(p.largest), d3.format(",")(Math.round(p.peak)), ratio(p.peak / p.largest)]));
}

// ── Chart: fee structures ──────────────────────────────────────────────

function feeChart(el, fees) {
  const tip = tooltip(el);
  const profit = FEE_ORDER.map((k) => {
    const v = fees[k].profit_vs_production;
    return { label: FEE_NAMES[k], lo: v.p5, mid: v.p50, hi: v.p95, text: ratio(v.p50),
      tip: [FEE_NAMES[k], `profit ${ratio(v.p50)} the current fee`, `90% of worlds ${ratio(v.p5)} to ${ratio(v.p95)}`] };
  });
  const refunds = FEE_ORDER.map((k) => {
    const v = fees[k].refunds_per_1000_paid;
    const r = fees[k].no_route_per_1000_paid;
    return { label: FEE_NAMES[k], lo: v.p5, mid: v.p50, hi: v.p95, text: per1000(v.p50),
      tip: [FEE_NAMES[k], `${per1000(v.p50)} refunds per 1000 paid sales`, `90% of worlds ${per1000(v.p5)} to ${per1000(v.p95)}`,
        `no route within the fee: up to ${per1000(r.p95)} per 1000`] };
  });
  const h1 = 24 + profit.length * ROW + 34 + 18;
  const h2 = 24 + refunds.length * ROW + 34;
  const svg = svgFor(el, h1 + h2, "Profit and refunds per fee structure, median and 90% of worlds");
  const xp = d3.scaleLinear().domain([0, 1.8]).range([LEFT, W - RIGHT]);
  rangeRows(svg, tip, 0, profit, xp, { title: "Profit, compared with the current fee", ticks: [0, 0.5, 1, 1.5], axisFormat: (d) => `${d}×`, ref: 1 });
  const xr = d3.scaleLinear().domain([0, 90]).range([LEFT, W - RIGHT]);
  rangeRows(svg, tip, h1, refunds, xr, { title: "Refunds per 1000 paid sales", ticks: [0, 20, 40, 60, 80], axisFormat: (d) => d });
  table(el, ["fee", "profit vs now (median, 90%)", "refunds per 1000 (median, 90%)", "no route within fee per 1000, p95", "losing days"],
    FEE_ORDER.map((k) => {
      const f = fees[k];
      return [FEE_NAMES[k], `${ratio(f.profit_vs_production.p50)} (${ratio(f.profit_vs_production.p5)}–${ratio(f.profit_vs_production.p95)})`,
        `${per1000(f.refunds_per_1000_paid.p50)} (${per1000(f.refunds_per_1000_paid.p5)}–${per1000(f.refunds_per_1000_paid.p95)})`,
        per1000(f.no_route_per_1000_paid.p95), `${f.losing_days} of ${f.days}`];
    }));
}

// ── Chart: time in flight ──────────────────────────────────────────────

function flightChart(el, counts, bins) {
  const total = d3.sum(counts);
  const label = (i) => (i === 0 ? `under ${duration(bins[1])}` : `${duration(bins[i])} to ${duration(bins[i + 1])}`);
  const rows = counts.map((n, i) => ({ n, i })).filter((r) => r.n > 0).map(({ n, i }) => ({
    label: label(i), value: n / total, text: n / total < 0.01 ? pct(n / total, 2) : pct(n / total),
    tip: [label(i), `${d3.format(",")(n)} of ${d3.format(",")(total)} paid sales`],
  }));
  const tip = tooltip(el);
  const height = rows.length * ROW + 8;
  const svg = svgFor(el, height, "Share of paid sales by time from payment to the sale's end");
  const x = d3.scaleLinear().domain([0, d3.max(rows, (r) => r.value)]).range([LEFT, W - RIGHT - 30]);
  barRows(svg, tip, 0, rows, x, { ticks: [] });
  table(el, ["time in flight", "paid sales", "share"], rows.map((r) => [r.label, r.tip[1].split(" of ")[0], r.text]));
}

// ── Chart: what drives the outcomes ────────────────────────────────────

function driversChart(el, drivers) {
  const tip = tooltip(el);
  const blocks = Object.entries(drivers).map(([outcome, corr]) => ({
    outcome,
    rows: Object.entries(corr).slice(0, 4).map(([p, r]) => ({
      label: PARAM_NAMES[p] ?? p, value: r, text: signed(r),
      tip: [`${OUTCOME_NAMES[outcome]}`, `${PARAM_NAMES[p] ?? p}: rank correlation ${signed(r)}`],
    })),
  }));
  const heights = blocks.map((b, i) => 24 + b.rows.length * ROW + (i === blocks.length - 1 ? 34 : 8) + 14);
  const svg = svgFor(el, d3.sum(heights), "The strongest rank correlations between the world's assumptions and each outcome");
  const x = d3.scaleLinear().domain([-1, 1]).range([LEFT + 30, W - RIGHT]);
  let top = 0;
  blocks.forEach((b, i) => {
    barRows(svg, tip, top, b.rows, x, { title: OUTCOME_NAMES[b.outcome], ticks: [-1, -0.5, 0, 0.5, 1],
      axisFormat: i === blocks.length - 1 ? (d) => String(d).replace("-", "−") : null });
    top += heights[i];
  });
  table(el, ["outcome", "assumption", "rank correlation"],
    blocks.flatMap((b) => b.rows.map((r) => [OUTCOME_NAMES[b.outcome], r.label, r.text])));
}

// ── Text ───────────────────────────────────────────────────────────────

function fill(name, value) {
  document.querySelectorAll(`[data-fill="${name}"]`).forEach((el) => { el.textContent = value; });
}

function fillText(d) {
  const f = d.fronted;
  const o = d.operations;
  const fees = d.fees;
  const n = d3.format(",");
  fill("worlds", d.worlds);
  fill("runs", d.worlds * d.fee_structures.length);
  fill("sales-total", n(d.sales_total));
  fill("problems", d.problems);
  fill("ledger-broken", d.ledger_broken);
  fill("fronted-payments", n(f.payments));
  fill("fronted-s-p50", Math.round(f.seconds.p50));
  fill("fronted-s-p99", Math.round(f.seconds.p99));
  fill("fronted-s-max", duration(f.seconds.p100));
  fill("fronted-peak-p50", sats(f.peak_sats.p50));
  fill("fronted-peak-p95", sats(f.peak_sats.p95));
  fill("fronted-over-largest-p50", ratio(f.peak_over_largest_sale.p50));
  fill("fronted-over-largest-max", ratio(f.peak_over_largest_sale.p100));
  fill("fronted-end", n(f.end_sats_max));
  fill("weeks-at-cap", d.points.fronted.filter((p) => p.largest >= 2e6).length);
  fill("profit-15", ratio(fees["0:15000"].profit_vs_production.p50));
  fill("profit-05", ratio(fees["1000:5000"].profit_vs_production.p50));
  fill("profit-5-05", ratio(fees["5000:5000"].profit_vs_production.p50));
  fill("profit-share-now", pct(fees[PRODUCTION].profit_share_of_volume.p50, 2));
  fill("losing-days", d3.sum(Object.values(fees), (x) => x.losing_days));
  fill("fee-days", d3.sum(Object.values(fees), (x) => x.days));
  fill("refunds-p50", per1000(o.refunds_per_1000_paid.p50));
  fill("refunds-p90", per1000(o.refunds_per_1000_paid.p90));
  fill("refunds-p99", per1000(o.refunds_per_1000_paid.p99));
  fill("no-route-05-p95", per1000(fees["1000:5000"].no_route_per_1000_paid.p95));
  fill("no-route-now-p95", per1000(fees[PRODUCTION].no_route_per_1000_paid.p95));
  fill("attention-p50", o.attention_per_1000_sales.p50.toFixed(2));
  fill("attention-p99", o.attention_per_1000_sales.p99.toFixed(1));
  fill("refused-p50", o.refused_per_1000.p50.toFixed(1));
  fill("flight-p50", Math.round(o.in_flight_s.p50));
  fill("flight-p90", Math.round(o.in_flight_s.p90));
  fill("flight-p99", duration(o.in_flight_s.p99));
  fill("flight-p999", duration(o.in_flight_s["p99.9"]));
  fill("stuck-p50", Math.round(o.stuck_max_per_week.p50));
  fill("stuck-p90", Math.round(o.stuck_max_per_week.p90));
  fill("stuck-p99", Math.round(o.stuck_max_per_week.p99));
  const r = d.drivers;
  fill("rho-reserve", r.refunds_per_1000_paid.reserve_rejected.toFixed(2));
  fill("rho-payout-fail", r.refunds_per_1000_paid.payout_fail_base.toFixed(2));
  fill("rho-stuck", r.stuck_minutes.payout_stuck.toFixed(2));
  fill("rho-ppm", signed(r.profit_share_of_volume.routing_ppm));
}

async function main() {
  const d = await (await fetch("data.json")).json();
  fillText(d);
  frontedChart(document.getElementById("chart-fronted"), d.points.fronted);
  feeChart(document.getElementById("chart-fees"), d.fees);
  flightChart(document.getElementById("chart-flight"), d.points.in_flight_s, d.points.in_flight_bins);
  driversChart(document.getElementById("chart-drivers"), d.drivers);
}

document.getElementById("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
});

main();
