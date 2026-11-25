// Interface front NetAuto Studio — Étapes 1 à 3 (Lecture + Mode Écriture)

const ROLE_STYLES = {
  router: { bg: "#172554", border: "#3b82f6", accent: "#60a5fa", label: "Routeur", tag: "RTR" },
  switch: { bg: "#064e3b", border: "#10b981", accent: "#34d399", label: "Switch", tag: "SW" },
  container: { bg: "#451a03", border: "#f59e0b", accent: "#fbbf24", label: "Client", tag: "HOST" },
  unknown: { bg: "#1e293b", border: "#64748b", accent: "#94a3b8", label: "Autre", tag: "NODE" },
};

let network = null;
let currentTopology = null; // Topologie en cours (lue ou brouillon)
let isDraftLab = false;     // true si le lab n'existe pas encore dans NetBox
let linkMode = false;
let linkSourceId = null;
let selectedItem = null;    // { type: "node"|"link", id: ... }
let nextLinkTempId = 1000;

const labSelect = document.getElementById("lab-select");
const modeBadge = document.getElementById("mode-badge");
const editorToolbar = document.getElementById("editor-toolbar");
const statusBanner = document.getElementById("status-banner");
const labSummary = document.getElementById("lab-summary");
const detailsPanel = document.getElementById("details-panel");
const btnLinkMode = document.getElementById("btn-link-mode");
const btnDeleteSelected = document.getElementById("btn-delete-selected");
const btnSaveNetbox = document.getElementById("btn-save-netbox");
const btnPreview = document.getElementById("btn-preview");
const btnApply = document.getElementById("btn-apply");
const btnDestroyLab = document.getElementById("btn-destroy-lab");
const btnToggleConsole = document.getElementById("btn-toggle-console");
const btnCloseConsole = document.getElementById("btn-close-console");
const consoleDrawer = document.getElementById("console-drawer");
const consoleTitleText = document.getElementById("console-title-text");
const consoleSummary = document.getElementById("console-summary");
const consoleSteps = document.getElementById("console-steps");
const consoleLogs = document.getElementById("console-logs");
const linkHint = document.getElementById("link-hint");
const modalNewLab = document.getElementById("modal-new-lab");
const formNewLab = document.getElementById("form-new-lab");
const inputLabName = document.getElementById("input-lab-name");
const selectTemplate = document.getElementById("select-template");

let activeEventSource = null;

let bannerTimer = null;

function showBanner(msg, type = "error") {
  clearTimeout(bannerTimer);
  statusBanner.textContent = msg;
  statusBanner.classList.remove("hidden", "success");
  if (type === "success") {
    statusBanner.classList.add("success");
    bannerTimer = setTimeout(() => statusBanner.classList.add("hidden"), 4500);
  }
}

function clearBanner() {
  clearTimeout(bannerTimer);
  statusBanner.classList.add("hidden");
}

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function isEditable() {
  return Boolean(currentTopology && !currentTopology.readonly);
}

function setLinkMode(active) {
  linkMode = active;
  linkSourceId = null;
  btnLinkMode.classList.toggle("btn-active", active);
  linkHint.classList.toggle("hidden", !active);
  if (active) linkHint.textContent = "Cliquez sur le 1er nœud à relier...";
}

function updateModeUI() {
  if (!currentTopology) return;
  modeBadge.classList.remove("hidden", "badge-readonly", "badge-edit", "badge-draft");
  if (currentTopology.readonly) {
    modeBadge.textContent = "Lecture seule";
    modeBadge.classList.add("badge-readonly");
    editorToolbar.classList.add("hidden");
    setLinkMode(false);
  } else if (isDraftLab) {
    modeBadge.textContent = `Brouillon : ${currentTopology.name}`;
    modeBadge.classList.add("badge-draft");
    editorToolbar.classList.remove("hidden");
  } else {
    modeBadge.textContent = "Mode édition";
    modeBadge.classList.add("badge-edit");
    editorToolbar.classList.remove("hidden");
  }
  btnDeleteSelected.disabled = !isEditable() || !selectedItem;
  const canDestroy = Boolean(currentTopology && currentTopology.name !== "netauto" && currentTopology.deletable !== false);
  btnDestroyLab.classList.toggle("hidden", !canDestroy);
}

function buildNodeTooltip(node) {
  const lines = [
    `${node.id} (${(ROLE_STYLES[node.role] || ROLE_STYLES.unknown).label})`,
    `Mgmt : ${node.mgmt_ip || "auto"}`,
  ];
  for (const itf of node.interfaces || []) {
    if (!itf.mgmt_only) {
      const ips = itf.ips?.length ? itf.ips.join(", ") : "L2";
      lines.push(`${itf.name} : ${ips}`);
    }
  }
  const asn = node.asn ?? node.context?.asn;
  const vrf = node.mac_vrf ?? node.context?.mac_vrf;
  if (asn) lines.push(`AS : ${asn}`);
  if (vrf) lines.push(`mac-vrf : ${vrf}`);
  return lines.join("\n");
}

function buildNodeLabel(node) {
  const st = ROLE_STYLES[node.role] || ROLE_STYLES.unknown;
  const asn = node.asn ?? node.context?.asn;
  const vrf = node.mac_vrf ?? node.context?.mac_vrf;
  const sub = asn
    ? `AS ${asn}`
    : vrf
      ? vrf
      : (node.mgmt_ip ? node.mgmt_ip.split("/")[0] : st.tag);
  return `${node.id}\n${sub}`;
}

function renderSummary(topo) {
  const counts = { router: 0, switch: 0, container: 0 };
  for (const n of topo.nodes) {
    if (counts[n.role] !== undefined) counts[n.role] += 1;
  }
  labSummary.innerHTML = `
    <div class="stat"><span class="stat-label">Routeurs</span><span class="stat-value">${counts.router}</span></div>
    <div class="stat"><span class="stat-label">Switchs</span><span class="stat-value">${counts.switch}</span></div>
    <div class="stat"><span class="stat-label">Clients</span><span class="stat-value">${counts.container}</span></div>
    <div class="stat"><span class="stat-label">Liens</span><span class="stat-value">${topo.links.length}</span></div>
  `;
}

function renderNodeDetails(node) {
  const style = ROLE_STYLES[node.role] || ROLE_STYLES.unknown;
  if (!isEditable()) {
    let html = `
      <div class="prop-list">
        <div class="prop-row"><span class="prop-key">Nœud</span><span class="prop-val">${escapeHtml(node.id)}</span></div>
        <div class="prop-row"><span class="prop-key">NetBox</span><span class="prop-val">${escapeHtml(node.device_name || "")}</span></div>
        <div class="prop-row"><span class="prop-key">Rôle</span><span class="prop-val">${escapeHtml(style.label)}</span></div>
        <div class="prop-row"><span class="prop-key">Mgmt IP</span><span class="prop-val">${escapeHtml(node.mgmt_ip || "—")}</span></div>
    `;
    if (node.context?.asn) html += `<div class="prop-row"><span class="prop-key">ASN</span><span class="prop-val">${escapeHtml(node.context.asn)}</span></div>`;
    if (node.context?.router_id) html += `<div class="prop-row"><span class="prop-key">Router ID</span><span class="prop-val">${escapeHtml(node.context.router_id)}</span></div>`;
    if (node.context?.mac_vrf) html += `<div class="prop-row"><span class="prop-key">mac-vrf</span><span class="prop-val">${escapeHtml(node.context.mac_vrf)}</span></div>`;
    html += renderInterfacesAndBgp(node);
    html += `</div>`;
    detailsPanel.innerHTML = html;
    return;
  }

  // Formulaire d'édition (laisser vide = allocation automatique IPAM/ASN)
  const curAsn = node.asn ?? node.context?.asn ?? "";
  const curRid = node.router_id ?? node.context?.router_id ?? "";
  const curVrf = node.mac_vrf ?? node.context?.mac_vrf ?? "";
  const curMgmt = node.mgmt_ip ?? "";
  const dataIntf = (node.interfaces || []).find((i) => !i.mgmt_only && i.ips?.length);
  const curDataIp = node.data_ip ?? (node.role === "container" && dataIntf ? dataIntf.ips[0] : "");

  let html = `
    <div class="prop-list">
      <div class="prop-row"><span class="prop-key">Rôle</span><span class="prop-val">${escapeHtml(style.label)}</span></div>
      <div class="form-field">
        <label>Nom du nœud</label>
        <input id="edit-node-id" type="text" value="${escapeHtml(node.id)}" />
      </div>
      <div class="form-field">
        <label>IP Management (vide = auto IPAM)</label>
        <input id="edit-node-mgmt" type="text" value="${escapeHtml(curMgmt)}" placeholder="Auto (ex. 172.20.32.11/24)" />
      </div>
  `;

  if (node.role === "router") {
    html += `
      <div class="form-field">
        <label>ASN (vide = auto dès 65001)</label>
        <input id="edit-node-asn" type="number" value="${escapeHtml(curAsn)}" placeholder="Auto" />
      </div>
      <div class="form-field">
        <label>Router ID (vide = auto)</label>
        <input id="edit-node-rid" type="text" value="${escapeHtml(curRid)}" placeholder="Auto" />
      </div>
    `;
  } else if (node.role === "switch") {
    html += `
      <div class="form-field">
        <label>mac-vrf (vide = auto lan-N)</label>
        <input id="edit-node-vrf" type="text" value="${escapeHtml(curVrf)}" placeholder="Auto" />
      </div>
    `;
  } else if (node.role === "container") {
    html += `
      <div class="form-field">
        <label>IP eth1 (vide = auto .10+)</label>
        <input id="edit-node-dataip" type="text" value="${escapeHtml(curDataIp)}" placeholder="Auto" />
      </div>
    `;
  }

  html += `<button id="btn-apply-node" type="button" class="btn btn-secondary">Appliquer au brouillon</button>`;
  html += renderInterfacesAndBgp(node);
  html += `</div>`;
  detailsPanel.innerHTML = html;

  document.getElementById("btn-apply-node").addEventListener("click", () => {
    const newId = document.getElementById("edit-node-id").value.trim();
    if (!newId) return;
    if (newId !== node.id) {
      if (currentTopology.nodes.some((n) => n.id === newId)) {
        showBanner(`Un nœud nommé '${newId}' existe déjà.`);
        return;
      }
      for (const l of currentTopology.links) {
        if (l.from === node.id) l.from = newId;
        if (l.to === node.id) l.to = newId;
      }
      node.id = newId;
      selectedItem = { type: "node", id: newId };
    }
    const mgmtVal = document.getElementById("edit-node-mgmt").value.trim();
    node.mgmt_ip = mgmtVal || null;
    if (node.role === "router") {
      const asnVal = document.getElementById("edit-node-asn").value.trim();
      const ridVal = document.getElementById("edit-node-rid").value.trim();
      node.asn = asnVal ? parseInt(asnVal, 10) : null;
      node.router_id = ridVal || null;
    } else if (node.role === "switch") {
      const vrfVal = document.getElementById("edit-node-vrf").value.trim();
      node.mac_vrf = vrfVal || null;
    } else if (node.role === "container") {
      const dataVal = document.getElementById("edit-node-dataip").value.trim();
      node.data_ip = dataVal || null;
    }
    clearBanner();
    renderGraph(currentTopology);
    renderNodeDetails(node);
  });
}

function renderInterfacesAndBgp(node) {
  let html = "";
  if (Array.isArray(node.interfaces) && node.interfaces.length) {
    html += `<div class="sub-title">Interfaces NetBox</div>`;
    for (const itf of node.interfaces) {
      const label = itf.ips?.length ? itf.ips.join(", ") : (itf.mgmt_only ? "mgmt" : "bridged L2");
      html += `<div class="intf-item"><span>${escapeHtml(itf.name)}</span><span>${escapeHtml(label)}</span></div>`;
    }
  }
  if (Array.isArray(node.context?.bgp_neighbors) && node.context.bgp_neighbors.length) {
    html += `<div class="sub-title">Voisins BGP (dérivés)</div>`;
    for (const n of node.context.bgp_neighbors) {
      html += `<div class="intf-item"><span>${escapeHtml(n.ip)}</span><span>AS ${escapeHtml(n.peer_as)}</span></div>`;
    }
  }
  return html;
}

function renderLinkDetails(link) {
  const fromLabel = link.from_interface ? `${link.from}:${link.from_interface}` : link.from;
  const toLabel = link.to_interface ? `${link.to}:${link.to_interface}` : link.to;
  detailsPanel.innerHTML = `
    <div class="prop-list">
      <div class="prop-row"><span class="prop-key">Extrémité A</span><span class="prop-val">${escapeHtml(fromLabel)}</span></div>
      <div class="prop-row"><span class="prop-key">Extrémité B</span><span class="prop-val">${escapeHtml(toLabel)}</span></div>
    </div>
  `;
}

function computeLayeredPositions(topo) {
  const adjacency = new Map();
  for (const n of topo.nodes) adjacency.set(n.id, new Set());
  for (const l of topo.links) {
    adjacency.get(l.from)?.add(l.to);
    adjacency.get(l.to)?.add(l.from);
  }

  const routers = topo.nodes.filter((n) => n.role === "router");
  const switches = topo.nodes.filter((n) => n.role === "switch");
  const clients = topo.nodes.filter((n) => n.role === "container");
  const others = topo.nodes.filter((n) => !["router", "switch", "container"].includes(n.role));

  const pos = new Map();
  const X_STEP = 230;
  const Y_ROUTER = -150;
  const Y_SWITCH = 0;
  const Y_CLIENT = 150;

  const rStart = -((routers.length - 1) * X_STEP) / 2;
  routers.forEach((r, idx) => pos.set(r.id, { x: rStart + idx * X_STEP, y: Y_ROUTER }));

  const swBuckets = new Map();
  const unattachedSw = [];
  for (const sw of switches) {
    const parentRouter = routers.find((r) => adjacency.get(sw.id)?.has(r.id));
    if (parentRouter) {
      if (!swBuckets.has(parentRouter.id)) swBuckets.set(parentRouter.id, []);
      swBuckets.get(parentRouter.id).push(sw);
    } else {
      unattachedSw.push(sw);
    }
  }
  for (const [rId, group] of swBuckets.entries()) {
    const cx = pos.get(rId).x;
    const start = cx - ((group.length - 1) * 160) / 2;
    group.forEach((sw, i) => pos.set(sw.id, { x: start + i * 160, y: Y_SWITCH }));
  }
  unattachedSw.forEach((sw, i) => {
    const start = -((unattachedSw.length - 1) * X_STEP) / 2;
    pos.set(sw.id, { x: start + i * X_STEP, y: Y_SWITCH });
  });

  const clBuckets = new Map();
  const unattachedCl = [];
  for (const cl of clients) {
    const parent = [...switches, ...routers].find((p) => adjacency.get(cl.id)?.has(p.id));
    if (parent && pos.has(parent.id)) {
      if (!clBuckets.has(parent.id)) clBuckets.set(parent.id, []);
      clBuckets.get(parent.id).push(cl);
    } else {
      unattachedCl.push(cl);
    }
  }
  for (const [pId, group] of clBuckets.entries()) {
    const cx = pos.get(pId).x;
    const start = cx - ((group.length - 1) * 150) / 2;
    group.forEach((cl, i) => pos.set(cl.id, { x: start + i * 150, y: Y_CLIENT }));
  }
  unattachedCl.forEach((cl, i) => {
    const start = -((unattachedCl.length - 1) * X_STEP) / 2;
    pos.set(cl.id, { x: start + i * X_STEP, y: Y_CLIENT });
  });

  others.forEach((n, i) => pos.set(n.id, { x: i * 160, y: Y_CLIENT + 130 }));
  return pos;
}

function renderGraph(topo) {
  const positions = computeLayeredPositions(topo);

  const nodes = new vis.DataSet(
    topo.nodes.map((n) => {
      const st = ROLE_STYLES[n.role] || ROLE_STYLES.unknown;
      const xy = positions.get(n.id) || { x: 0, y: 0 };
      return {
        id: n.id,
        label: buildNodeLabel(n),
        title: buildNodeTooltip(n),
        x: xy.x,
        y: xy.y,
        shape: "box",
        margin: { top: 10, right: 14, bottom: 10, left: 14 },
        borderWidth: 2,
        borderWidthSelected: 3,
        shapeProperties: { borderRadius: 6 },
        color: {
          background: st.bg,
          border: st.border,
          hover: { background: st.bg, border: st.accent },
          highlight: { background: st.bg, border: "#ffffff" },
        },
        font: {
          color: "#f8fafc",
          size: 13,
          face: "ui-monospace, SFMono-Regular, Consolas, monospace",
        },
        shadow: { enabled: true, color: "rgba(0,0,0,0.35)", size: 8, x: 0, y: 3 },
      };
    })
  );

  const edges = new vis.DataSet(
    topo.links.map((l) => ({
      id: `link-${l.id}`,
      from: l.from,
      to: l.to,
      title: l.from_interface
        ? `${l.from}:${l.from_interface} ↔ ${l.to}:${l.to_interface}`
        : `${l.from} ↔ ${l.to}`,
      color: { color: "#475569", hover: "#94a3b8", highlight: "#e2e8f0" },
      width: 2,
      smooth: false,
    }))
  );

  const container = document.getElementById("network-graph");
  const options = {
    physics: { enabled: false },
    interaction: { hover: true, tooltipDelay: 120, dragNodes: true, dragView: true, zoomView: true },
  };

  if (network) network.destroy();
  network = new vis.Network(container, { nodes, edges }, options);
  if (topo.nodes.length) network.fit({ animation: false });

  network.on("click", (params) => {
    if (linkMode && isEditable() && params.nodes.length) {
      handleLinkClick(params.nodes[0]);
      return;
    }

    if (params.nodes.length) {
      const node = topo.nodes.find((n) => n.id === params.nodes[0]);
      selectedItem = node ? { type: "node", id: node.id } : null;
      if (node) renderNodeDetails(node);
    } else if (params.edges.length) {
      const link = topo.links.find((l) => `link-${l.id}` === params.edges[0]);
      selectedItem = link ? { type: "link", id: link.id } : null;
      if (link) renderLinkDetails(link);
    } else {
      selectedItem = null;
      detailsPanel.innerHTML = `<p class="muted">Cliquez sur un nœud ou un lien dans le graphe pour inspecter ses propriétés.</p>`;
    }
    updateModeUI();
  });
}

function handleLinkClick(nodeId) {
  if (!linkSourceId) {
    linkSourceId = nodeId;
    linkHint.textContent = `Source : ${nodeId} — cliquez sur le 2e nœud...`;
    return;
  }
  if (linkSourceId === nodeId) {
    showBanner("Impossible de relier un nœud à lui-même.");
    setLinkMode(false);
    return;
  }
  const exists = currentTopology.links.some(
    (l) => (l.from === linkSourceId && l.to === nodeId) || (l.from === nodeId && l.to === linkSourceId)
  );
  if (exists) {
    showBanner(`Un lien existe déjà entre ${linkSourceId} et ${nodeId}.`);
    setLinkMode(false);
    return;
  }
  currentTopology.links.push({
    id: nextLinkTempId++,
    from: linkSourceId,
    to: nodeId,
    from_interface: "",
    to_interface: "",
  });
  clearBanner();
  setLinkMode(false);
  renderSummary(currentTopology);
  renderGraph(currentTopology);
}

function nextNodeName(prefix) {
  let i = 1;
  while (currentTopology.nodes.some((n) => n.id === `${prefix}${i}`)) i += 1;
  return `${prefix}${i}`;
}

function addNode(role) {
  if (!isEditable()) return;
  const prefix = role === "router" ? "r" : role === "switch" ? "sw" : "client";
  const node = {
    id: nextNodeName(prefix),
    role,
    mgmt_ip: null,
    asn: null,
    router_id: null,
    mac_vrf: null,
    data_ip: null,
    interfaces: [],
    context: {},
  };
  currentTopology.nodes.push(node);
  selectedItem = { type: "node", id: node.id };
  clearBanner();
  renderSummary(currentTopology);
  renderGraph(currentTopology);
  renderNodeDetails(node);
  updateModeUI();
}

function deleteSelected() {
  if (!isEditable() || !selectedItem) return;
  if (selectedItem.type === "node") {
    currentTopology.nodes = currentTopology.nodes.filter((n) => n.id !== selectedItem.id);
    currentTopology.links = currentTopology.links.filter(
      (l) => l.from !== selectedItem.id && l.to !== selectedItem.id
    );
  } else if (selectedItem.type === "link") {
    currentTopology.links = currentTopology.links.filter((l) => l.id !== selectedItem.id);
  }
  selectedItem = null;
  detailsPanel.innerHTML = `<p class="muted">Élément supprimé du brouillon.</p>`;
  renderSummary(currentTopology);
  renderGraph(currentTopology);
  updateModeUI();
}

function buildSavePayload() {
  return {
    name: currentTopology.name,
    nodes: currentTopology.nodes.map((n) => ({
      id: n.id,
      role: n.role,
      mgmt_ip: n.mgmt_ip || null,
      asn: n.asn ?? n.context?.asn ?? null,
      router_id: n.router_id ?? n.context?.router_id ?? null,
      mac_vrf: n.mac_vrf ?? n.context?.mac_vrf ?? null,
      data_ip: n.data_ip || null,
    })),
    links: currentTopology.links.map((l) => ({ from: l.from, to: l.to })),
  };
}

async function saveToNetBox() {
  if (!isEditable()) return;
  clearBanner();
  btnSaveNetbox.disabled = true;
  btnSaveNetbox.textContent = "Enregistrement...";
  try {
    const method = isDraftLab ? "POST" : "PUT";
    const url = isDraftLab ? "api/labs" : `api/labs/${encodeURIComponent(currentTopology.name)}`;
    const resp = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildSavePayload()),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || `Erreur HTTP ${resp.status}`);
    }
    const savedName = data.name;
    isDraftLab = false;
    await refreshLabList(savedName);
    showBanner(`Lab '${savedName}' enregistré dans NetBox (IPAM, ASN et BGP alloués).`, "success");
  } catch (err) {
    showBanner(err.message);
  } finally {
    btnSaveNetbox.disabled = false;
    btnSaveNetbox.textContent = "Enregistrer dans NetBox";
  }
}

function startNewDraftLab(labName, template) {
  const nodes = [];
  const links = [];
  if (template === "classic") {
    nodes.push(
      { id: "r1", role: "router", interfaces: [], context: {} },
      { id: "r2", role: "router", interfaces: [], context: {} },
      { id: "sw1", role: "switch", interfaces: [], context: {} },
      { id: "sw2", role: "switch", interfaces: [], context: {} },
      { id: "client1", role: "container", interfaces: [], context: {} },
      { id: "client2", role: "container", interfaces: [], context: {} }
    );
    links.push(
      { id: 1, from: "client1", to: "sw1", from_interface: "", to_interface: "" },
      { id: 2, from: "sw1", to: "r1", from_interface: "", to_interface: "" },
      { id: 3, from: "r1", to: "r2", from_interface: "", to_interface: "" },
      { id: 4, from: "r2", to: "sw2", from_interface: "", to_interface: "" },
      { id: 5, from: "sw2", to: "client2", from_interface: "", to_interface: "" }
    );
  }
  currentTopology = {
    name: labName,
    site_slug: labName,
    display_name: labName,
    readonly: false,
    nodes,
    links,
  };
  isDraftLab = true;
  selectedItem = null;
  clearBanner();
  updateModeUI();
  renderSummary(currentTopology);
  renderGraph(currentTopology);
  detailsPanel.innerHTML = `<p class="muted">Ajoutez des nœuds/liens puis cliquez sur <b>Enregistrer dans NetBox</b>.</p>`;
}

async function loadLab(labName) {
  clearBanner();
  try {
    const resp = await fetch(`api/labs/${encodeURIComponent(labName)}`);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `Erreur HTTP ${resp.status}`);
    }
    currentTopology = await resp.json();
    isDraftLab = false;
    selectedItem = null;
    updateModeUI();
    renderSummary(currentTopology);
    renderGraph(currentTopology);
    detailsPanel.innerHTML = `<p class="muted">Cliquez sur un nœud ou un lien dans le graphe pour inspecter ses propriétés.</p>`;
  } catch (err) {
    showBanner(`Impossible de charger le lab : ${err.message}`);
  }
}

async function refreshLabList(selectName = null) {
  clearBanner();
  try {
    const resp = await fetch("api/labs");
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `Erreur HTTP ${resp.status}`);
    }
    const labs = await resp.json();
    labSelect.innerHTML = "";
    for (const lab of labs) {
      const opt = document.createElement("option");
      opt.value = lab.name;
      opt.textContent = `${lab.name}${lab.readonly ? " (lecture seule)" : ""} — ${lab.device_count} nœuds`;
      labSelect.appendChild(opt);
    }
    const target = selectName || (labs.length ? labs[0].name : null);
    if (target) {
      labSelect.value = target;
      await loadLab(target);
    } else {
      showBanner("Aucun lab trouvé dans NetBox.");
    }
  } catch (err) {
    showBanner(`Erreur de connexion à l'API : ${err.message}`);
  }
}

// Événements
labSelect.addEventListener("change", () => loadLab(labSelect.value));
document.getElementById("btn-refresh").addEventListener("click", () => {
  if (!isDraftLab && labSelect.value) loadLab(labSelect.value);
  else refreshLabList();
});
document.getElementById("btn-fit").addEventListener("click", () => {
  if (network) network.fit({ animation: false });
});

document.getElementById("btn-add-router").addEventListener("click", () => addNode("router"));
document.getElementById("btn-add-switch").addEventListener("click", () => addNode("switch"));
document.getElementById("btn-add-client").addEventListener("click", () => addNode("container"));
btnLinkMode.addEventListener("click", () => setLinkMode(!linkMode));
btnDeleteSelected.addEventListener("click", deleteSelected);
function renderConsoleSteps(steps) {
  consoleSteps.innerHTML = steps
    .map((s, idx) => {
      const dur = s.duration ? ` (${s.duration}s)` : "";
      const icon = s.status === "ok" ? "✓" : s.status === "error" ? "✗" : s.status === "running" ? "⟳" : s.status === "skipped" ? "–" : "•";
      return `<span id="step-pill-${idx}" class="step-pill ${s.status}">${icon} ${escapeHtml(s.name)}${dur}</span>`;
    })
    .join("");
}

function setActionButtonsDisabled(disabled) {
  btnSaveNetbox.disabled = disabled;
  btnPreview.disabled = disabled;
  btnApply.disabled = disabled;
}

async function runJob(mode) {
  if (!currentTopology) return;
  if (isDraftLab && mode === "preview") {
    showBanner("Enregistrez d'abord le brouillon avant de lancer un Aperçu Ansible.");
    return;
  }
  clearBanner();
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
  }

  consoleDrawer.classList.remove("hidden");
  consoleLogs.textContent = "";
  consoleTitleText.textContent =
    mode === "preview"
      ? `Aperçu (dry-run) — ${currentTopology.name}`
      : `Déploiement — ${currentTopology.name}`;
  consoleSummary.textContent = "En cours...";
  setActionButtonsDisabled(true);

  try {
    const endpoint = `api/labs/${encodeURIComponent(currentTopology.name)}/${mode}`;
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildSavePayload()),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || `Erreur HTTP ${resp.status}`);
    }

    const steps = data.steps || [];
    renderConsoleSteps(steps);

    const es = new EventSource(`api/jobs/${encodeURIComponent(data.job_id)}/stream`);
    activeEventSource = es;

    es.addEventListener("step", (ev) => {
      const st = JSON.parse(ev.data);
      steps[st.index] = st;
      renderConsoleSteps(steps);
    });

    es.addEventListener("log", (ev) => {
      const item = JSON.parse(ev.data);
      consoleLogs.textContent += `${item.line}\n`;
      consoleLogs.scrollTop = consoleLogs.scrollHeight;
    });

    es.addEventListener("done", async (ev) => {
      const done = JSON.parse(ev.data);
      es.close();
      activeEventSource = null;
      setActionButtonsDisabled(false);
      const ok = done.status === "ok";
      consoleSummary.textContent = `${ok ? "Succès" : "Échec"} en ${done.duration}s — ${done.log_file}`;
      showBanner(
        ok
          ? `${mode === "preview" ? "Aperçu" : "Déploiement"} terminé avec succès (${done.duration}s).`
          : `${mode === "preview" ? "Aperçu" : "Déploiement"} en échec — consultez la console.`,
        ok ? "success" : "error"
      );
      if (ok && mode === "apply") {
        isDraftLab = false;
        await refreshLabList(currentTopology.name);
      }
    });

    es.onerror = () => {
      if (activeEventSource === es) {
        es.close();
        activeEventSource = null;
        setActionButtonsDisabled(false);
        consoleSummary.textContent = "Connexion interrompue";
      }
    };
  } catch (err) {
    setActionButtonsDisabled(false);
    consoleSummary.textContent = "Refusé";
    showBanner(err.message);
  }
}

async function destroyCurrentLab() {
  if (!currentTopology || currentTopology.name === "netauto") return;
  const name = currentTopology.name;
  if (isDraftLab) {
    if (!confirm(`Abandonner le brouillon '${name}' ?`)) return;
    isDraftLab = false;
    await refreshLabList();
    showBanner(`Brouillon '${name}' annulé.`, "success");
    return;
  }
  if (!confirm(`Détruire définitivement le lab '${name}' (conteneurs Containerlab + tous les objets NetBox) ?`)) {
    return;
  }
  clearBanner();
  setActionButtonsDisabled(true);
  btnDestroyLab.disabled = true;
  try {
    const resp = await fetch(`api/labs/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `Erreur HTTP ${resp.status}`);
    await refreshLabList();
    showBanner(`Lab '${name}' détruit (NetBox et conteneurs nettoyés).`, "success");
  } catch (err) {
    showBanner(`Erreur lors de la destruction : ${err.message}`);
  } finally {
    setActionButtonsDisabled(false);
    btnDestroyLab.disabled = false;
  }
}

btnSaveNetbox.addEventListener("click", saveToNetBox);
btnPreview.addEventListener("click", () => runJob("preview"));
btnApply.addEventListener("click", () => runJob("apply"));
btnDestroyLab.addEventListener("click", destroyCurrentLab);
btnToggleConsole.addEventListener("click", () => {
  consoleDrawer.classList.toggle("hidden");
  if (network) network.fit({ animation: false });
});
btnCloseConsole.addEventListener("click", () => {
  consoleDrawer.classList.add("hidden");
  if (network) network.fit({ animation: false });
});

document.getElementById("btn-new-lab").addEventListener("click", () => {
  inputLabName.value = "";
  modalNewLab.classList.remove("hidden");
  inputLabName.focus();
});
document.getElementById("btn-cancel-modal").addEventListener("click", () => {
  modalNewLab.classList.add("hidden");
});
formNewLab.addEventListener("submit", (e) => {
  e.preventDefault();
  const name = inputLabName.value.trim().toLowerCase();
  if (name === "netauto" || name === "tp-automatisation") {
    showBanner("Le nom 'netauto' est réservé au lab protégé.");
    return;
  }
  modalNewLab.classList.add("hidden");
  startNewDraftLab(name, selectTemplate.value);
});

refreshLabList();

// ---------------------------------------------------------------- Étape 6 : Onglets, Tests & Supervision Grafana
const viewTopology = document.getElementById("view-topology");
const viewTests = document.getElementById("view-tests");
const viewGrafana = document.getElementById("view-grafana");
const tabButtons = document.querySelectorAll(".tab-btn");

const pingSource = document.getElementById("ping-source");
const pingTarget = document.getElementById("ping-target");
const btnRunPing = document.getElementById("btn-run-ping");
const pingOutput = document.getElementById("ping-output");
const pingBadge = document.getElementById("ping-badge");

const btnRefreshStatus = document.getElementById("btn-refresh-status");
const liveStatusGrid = document.getElementById("live-status-grid");
const statusGlobalBadge = document.getElementById("status-global-badge");

const grafanaUrlInput = document.getElementById("grafana-url");
const grafanaIframe = document.getElementById("grafana-iframe");
const btnLoadGrafana = document.getElementById("btn-load-grafana");
const linkOpenGrafana = document.getElementById("link-open-grafana");
let grafanaLoaded = false;

function populatePingClients() {
  if (!currentTopology) return;
  const clients = currentTopology.nodes.filter((n) => n.role === "container");
  pingSource.innerHTML = "";
  pingTarget.innerHTML = "";
  for (const c of clients) {
    const o1 = document.createElement("option");
    o1.value = c.id;
    o1.textContent = c.id;
    pingSource.appendChild(o1);

    const o2 = document.createElement("option");
    o2.value = c.id;
    o2.textContent = c.id;
    pingTarget.appendChild(o2);
  }
  if (clients.length >= 2) {
    pingSource.value = clients[0].id;
    pingTarget.value = clients[1].id;
  }
  btnRunPing.disabled = clients.length < 2;
}

async function runPingTest() {
  if (!currentTopology) return;
  btnRunPing.disabled = true;
  pingBadge.classList.add("hidden");
  pingOutput.textContent = `Ping en cours : ${pingSource.value} → ${pingTarget.value}...`;
  try {
    const resp = await fetch(`api/labs/${encodeURIComponent(currentTopology.name)}/ping`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: pingSource.value, target: pingTarget.value, count: 3 }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `Erreur HTTP ${resp.status}`);

    pingOutput.textContent = data.output || "(aucune sortie)";
    pingBadge.textContent = data.ok ? `OK (${data.target_ip})` : `ÉCHEC (${data.target_ip})`;
    pingBadge.className = `badge ${data.ok ? "badge-edit" : "badge-readonly"}`;
  } catch (err) {
    pingOutput.textContent = `Erreur : ${err.message}`;
    pingBadge.textContent = "ERREUR";
    pingBadge.className = "badge badge-readonly";
  } finally {
    btnRunPing.disabled = false;
  }
}

async function loadLiveStatus() {
  if (!currentTopology) return;
  btnRefreshStatus.disabled = true;
  liveStatusGrid.innerHTML = `<p class="muted">Interrogation JSON-RPC des équipements SR Linux...</p>`;
  statusGlobalBadge.classList.add("hidden");
  try {
    const resp = await fetch(`api/labs/${encodeURIComponent(currentTopology.name)}/status`);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `Erreur HTTP ${resp.status}`);

    statusGlobalBadge.textContent = data.all_ok ? "TOUT EST CONFORME" : "ANOMALIE DÉTECTÉE";
    statusGlobalBadge.className = `badge ${data.all_ok ? "badge-edit" : "badge-readonly"}`;

    if (!data.devices.length) {
      liveStatusGrid.innerHTML = `<p class="muted">Aucun équipement SR Linux dans ce lab.</p>`;
      return;
    }

    liveStatusGrid.innerHTML = data.devices
      .map((dev) => {
        const reachPill = dev.reachable
          ? `<span class="state-pill state-ok">UP</span>`
          : `<span class="state-pill state-ko">OFFLINE</span>`;

        let body = "";
        if (!dev.reachable) {
          body = `<p class="muted">${escapeHtml(dev.error || "Injoignable")}</p>`;
        } else {
          body += `<div class="sub-title">Interfaces</div>`;
          for (const itf of dev.interfaces) {
            const cls = itf.ok ? "state-ok" : "state-ko";
            const ipTxt = itf.ips.length ? ` (${itf.ips[0]})` : "";
            body += `<div class="intf-item"><span>${escapeHtml(itf.name)}${escapeHtml(ipTxt)}</span><span class="state-pill ${cls}">${escapeHtml(itf.oper_state)}</span></div>`;
          }
          if (dev.role === "router") {
            body += `<div class="sub-title">Sessions BGP</div>`;
            if (!dev.bgp.length) {
              body += `<p class="muted">Aucun voisin BGP configuré.</p>`;
            }
            for (const b of dev.bgp) {
              const cls = b.ok ? "state-ok" : "state-ko";
              body += `<div class="intf-item"><span>${escapeHtml(b.peer_ip)} (AS ${escapeHtml(b.peer_as)})</span><span class="state-pill ${cls}">${escapeHtml(b.session_state)}</span></div>`;
            }
          }
        }
        return `
          <div class="dev-card">
            <div class="dev-card-head">
              <strong>${escapeHtml(dev.id)} <span class="muted">(${escapeHtml(dev.mgmt_ip)})</span></strong>
              ${reachPill}
            </div>
            ${body}
          </div>
        `;
      })
      .join("");
  } catch (err) {
    liveStatusGrid.innerHTML = `<p class="muted">Erreur : ${escapeHtml(err.message)}</p>`;
  } finally {
    btnRefreshStatus.disabled = false;
  }
}

function loadGrafanaIframe() {
  const url = grafanaUrlInput.value.trim() || "/grafana/?orgId=1&theme=dark";
  grafanaIframe.src = url;
  linkOpenGrafana.href = url;
  grafanaLoaded = true;
}

function switchView(viewName) {
  tabButtons.forEach((b) => b.classList.toggle("active", b.dataset.view === viewName));
  viewTopology.classList.toggle("hidden", viewName !== "topology");
  editorToolbar.classList.toggle("hidden", viewName !== "topology" || !isEditable());
  viewTests.classList.toggle("hidden", viewName !== "tests");
  viewGrafana.classList.toggle("hidden", viewName !== "grafana");

  if (viewName === "topology" && network) {
    network.fit({ animation: false });
  } else if (viewName === "tests") {
    populatePingClients();
    loadLiveStatus();
  } else if (viewName === "grafana" && !grafanaLoaded) {
    loadGrafanaIframe();
  }
}

tabButtons.forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
btnRunPing.addEventListener("click", runPingTest);
btnRefreshStatus.addEventListener("click", loadLiveStatus);
btnLoadGrafana.addEventListener("click", loadGrafanaIframe);
