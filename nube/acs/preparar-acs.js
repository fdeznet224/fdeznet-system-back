// Deja la base del ACS del ISP lista antes de arrancar GenieACS (se corre en
// cada arranque; lo que ya estaba igual no cambia):
// - cwmp.auth: usuario y clave con los que se reportan los módems.
// - cwmp.connectionRequestAuth: con los que el ACS los "llama" al momento.
// - provisión wispay-alta en el primer reporte (0 BOOTSTRAP): les pone esas
//   credenciales de llamada y el reporte cada 5 minutos (igual que producción).
const { MongoClient } = require("/usr/local/lib/node_modules/genieacs/node_modules/mongodb");

function requerida(nombre) {
  const valor = (process.env[nombre] || "").trim();
  if (!/^[A-Za-z0-9._-]+$/.test(valor)) {
    console.error(`[acs] falta ${nombre} (solo letras, números, punto, guion y guion bajo)`);
    process.exit(1);
  }
  return valor;
}

const usuario = requerida("ACS_USUARIO");
const clave = requerida("ACS_CLAVE");
const crUsuario = requerida("CR_USUARIO");
const crClave = requerida("CR_CLAVE");
const intervalo = parseInt(process.env.ACS_INTERVALO || "300", 10) || 300;

const PROVISION = `// WisPay: credenciales del aviso al momento (connection request) y reporte
// periódico; se fijan en el primer reporte del equipo a este ACS.
const ahora = Date.now();
declare("InternetGatewayDevice.ManagementServer.ConnectionRequestUsername", {value: ahora}, {value: ${JSON.stringify(crUsuario)}});
declare("InternetGatewayDevice.ManagementServer.ConnectionRequestPassword", {value: ahora}, {value: ${JSON.stringify(crClave)}});
declare("InternetGatewayDevice.ManagementServer.PeriodicInformEnable", {value: ahora}, {value: true});
declare("InternetGatewayDevice.ManagementServer.PeriodicInformInterval", {value: ahora}, {value: ${intervalo}});
`;

async function main() {
  const url = process.env.GENIEACS_MONGODB_CONNECTION_URL;
  let ultimoError;
  // Mongo puede estar arrancando todavía.
  for (let intento = 1; intento <= 30; intento++) {
    const cliente = new MongoClient(url, { serverSelectionTimeoutMS: 5000 });
    try {
      await cliente.connect();
      const db = cliente.db();
      const config = db.collection("config");
      await config.replaceOne({ _id: "cwmp.auth" }, { _id: "cwmp.auth", value: `AUTH("${usuario}", "${clave}")` }, { upsert: true });
      await config.replaceOne(
        { _id: "cwmp.connectionRequestAuth" },
        { _id: "cwmp.connectionRequestAuth", value: `AUTH("${crUsuario}", "${crClave}")` },
        { upsert: true },
      );
      await db.collection("provisions").replaceOne({ _id: "wispay-alta" }, { _id: "wispay-alta", script: PROVISION }, { upsert: true });
      await db.collection("presets").replaceOne(
        { _id: "wispay-alta" },
        {
          _id: "wispay-alta", weight: 0, channel: "alta", events: { "0 BOOTSTRAP": true }, precondition: "",
          configurations: [{ type: "provision", name: "wispay-alta", args: null }],
        },
        { upsert: true },
      );
      console.log("[acs] base lista");
      return;
    } catch (error) {
      ultimoError = error;
      console.error(`[acs] esperando la base (${intento}/30): ${error.message}`);
      await new Promise((r) => setTimeout(r, 3000));
    } finally {
      await cliente.close().catch(() => {});
    }
  }
  throw ultimoError;
}

main().catch((error) => {
  console.error("[acs] no se pudo preparar la base:", error.message);
  process.exit(1);
});
