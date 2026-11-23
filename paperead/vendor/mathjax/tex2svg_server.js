const fs = require("fs");
require("./mathjax-full/js/util/entities/all.js");
require("./mathjax-full/js/input/tex/ams/AmsConfiguration.js");
require("./mathjax-full/js/input/tex/mathtools/MathtoolsConfiguration.js");
require("./mathjax-full/js/input/tex/newcommand/NewcommandConfiguration.js");
require("./mathjax-full/js/input/tex/configmacros/ConfigMacrosConfiguration.js");
require("./mathjax-full/js/input/tex/noundefined/NoUndefinedConfiguration.js");
require("./mathjax-full/js/input/tex/boldsymbol/BoldsymbolConfiguration.js");
const { mathjax } = require("./mathjax-full/js/mathjax.js");
const { TeX } = require("./mathjax-full/js/input/tex.js");
const { SVG } = require("./mathjax-full/js/output/svg.js");
const { liteAdaptor } = require("./mathjax-full/js/adaptors/liteAdaptor.js");
const { RegisterHTMLHandler } = require("./mathjax-full/js/handlers/html.js");

const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);
const texInput = new TeX({
  packages: ["base", "ams", "mathtools", "newcommand", "configmacros", "noundefined", "boldsymbol"],
});
const svgOutput = new SVG({ fontCache: "none" });
const doc = mathjax.document("", { InputJax: texInput, OutputJax: svgOutput });

function normalizeSvg(svg) {
  const m = svg.match(/viewBox="([-\d.\s]+)"/);
  if (!m) return svg;
  const parts = m[1].trim().split(/\s+/).map(Number);
  if (parts.length !== 4) return svg;
  const width = parts[2];
  const height = parts[3];
  let out = svg.replace(/(<svg[^>]*?)\swidth="[^"]*"/, `$1 width="${width}pt"`);
  out = out.replace(/(<svg[^>]*?)\sheight="[^"]*"/, `$1 height="${height}pt"`);
  return out;
}

function render(id, tex, display) {
  try {
    const node = doc.convert(String(tex), { display: Boolean(display) });
    const raw = adaptor.innerHTML(node);
    if (!raw || raw.indexOf("<svg") < 0) {
      return { id: id, error: "empty svg" };
    }
    return { id: id, svg: normalizeSvg(raw) };
  } catch (err) {
    return { id: id, error: String(err && err.message ? err.message : err) };
  }
}

let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let idx;
  while ((idx = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    let req;
    try {
      req = JSON.parse(line);
    } catch (err) {
      process.stdout.write(JSON.stringify({ id: null, error: "bad json" }) + "\n");
      continue;
    }
    const result = render(req.id, req.tex, req.display);
    process.stdout.write(JSON.stringify(result) + "\n");
  }
});

process.stdin.on("end", () => process.exit(0));
process.on("SIGTERM", () => process.exit(0));