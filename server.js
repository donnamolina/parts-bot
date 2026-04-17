/**
 * Parts-Bot WhatsApp Server
 *
 * Receives supplier quote photos/PDFs via WhatsApp, extracts parts data,
 * searches international marketplaces in parallel, and returns a comparison
 * Excel file with landed costs.
 *
 * Stack: Baileys (WhatsApp Web API) + Python search engine
 * Port: 3002 (isolated from cancha-bot on 3000)
 */

require('dotenv').config();
const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
} = require('@whiskeysockets/baileys');
const pino = require('pino');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const Anthropic = require('@anthropic-ai/sdk');
const { createClient } = require('@supabase/supabase-js');

// ─── Anthropic + Supabase clients ───────────────────────────────────────────

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_ANON_KEY,
);

// ─── Config ──────────────────────────────────────────────────────────────────

const ALLOWED_NUMBERS = (process.env.ALLOWED_NUMBERS || '')
  .split(',')
  .map(n => n.trim().replace(/[^0-9]/g, ''))
  .filter(Boolean);

const AUTH_DIR = path.join(__dirname, 'auth_info');
const OUTPUT_DIR = path.join(__dirname, 'output');
const LOG_DIR = path.join(__dirname, 'logs');
const PYTHON = path.join(__dirname, '.venv', 'bin', 'python3');
const SONNET_MODEL = process.env.ANTHROPIC_SONNET_MODEL || "claude-sonnet-4-6";

// Ensure directories exist
[AUTH_DIR, OUTPUT_DIR, LOG_DIR].forEach(d => fs.mkdirSync(d, { recursive: true }));

// Logger
const logger = pino({ level: 'info' }, pino.destination(path.join(LOG_DIR, 'bot.log')));
const consoleLog = (...args) => { console.log(new Date().toISOString(), ...args); };

// ─── Conversation State ─────────────────────────────────────────────────────

/**
 * States: idle → awaiting_confirmation → searching → delivering → idle
 *
 * Each user has their own state stored in sessions map.
 */
const sessions = new Map();
const mediaQueues = new Map();   // num → [{ mediaPath, ext }, ...]
const queueDebounce = new Map(); // num → timerId
const textBuffers = new Map();   // num → { messages: [], timer }
const TEXT_BUFFER_MS = 2500;     // wait 2.5s for fragmented forwards before processing
const imageTimestamps = new Map(); // num → timestamp of last received image (for VIN pre-check)

// ─── Conversation History (idle chat layer) ─────────────────────────────────

const conversationHistories = new Map(); // jid → [{role, content}, ...]
const MAX_HISTORY_MESSAGES = 20;

function getHistory(jid) {
  if (!conversationHistories.has(jid)) {
    conversationHistories.set(jid, []);
  }
  return conversationHistories.get(jid);
}

function appendHistory(jid, role, content) {
  const history = getHistory(jid);
  history.push({ role, content });
  if (history.length > MAX_HISTORY_MESSAGES) {
    conversationHistories.set(jid, history.slice(-MAX_HISTORY_MESSAGES));
  }
}

function clearHistory(jid) {
  conversationHistories.set(jid, []);
}

function getSession(jid) {
  const num = jid.replace(/[^0-9]/g, '').replace(/@.*/, '');
  if (!sessions.has(num)) {
    sessions.set(num, {
      state: 'idle',
      extractedData: null,
      timeout: null,
      // reviewing state fields
      reviewResultsPath: null,   // path to stored results JSON
      reviewExcelPath: null,
      reviewVehicle: null,
      reviewExtraction: null,
      reviewSupplierTotal: null,
    });
  }
  return sessions.get(num);
}

function getMediaQueue(jid) {
  const num = jid.replace(/[^0-9]/g, '').replace(/@.*/, '');
  if (!mediaQueues.has(num)) mediaQueues.set(num, []);
  return mediaQueues.get(num);
}

function resetSession(jid) {
  const num = jid.replace(/[^0-9]/g, '').replace(/@.*/, '');
  const session = sessions.get(num);
  if (session && session.timeout) {
    clearTimeout(session.timeout);
  }
  sessions.set(num, {
    state: 'idle',
    extractedData: null,
    timeout: null,
    reviewResultsPath: null,
    reviewExcelPath: null,
    reviewVehicle: null,
    reviewExtraction: null,
    reviewSupplierTotal: null,
  });
  // If more PDFs are queued, process the next one
  if (_globalSock) {
    setImmediate(() => processNextQueued(_globalSock, jid));
  }
}

// ─── Language Detection ─────────────────────────────────────────────────────

function detectLanguage(text) {
  const lower = (text || '').toLowerCase();
  const spanishWords = ['hola', 'buenos', 'dale', 'confirmar', 'buscar', 'pieza',
    'vehiculo', 'precio', 'gracias', 'si', 'no', 'ok', 'bien', 'correcto'];
  const hits = spanishWords.filter(w => lower.includes(w)).length;
  return hits >= 1 ? 'es' : 'en';
}

const MSG = {
  es: {
    // welcome — handled by Sonnet conversational layer
    // send_photo — handled by Sonnet conversational layer
    not_allowed: '⛔ Tu número no está autorizado para usar este bot.',
    processing: '⏳ Procesando imagen...',
    confirm_prompt: '¿Todo correcto? Di *OK* o dime qué hay que ajustar 👀',
    searching: (n) => `🔍 Buscando ${n} piezas... esto toma entre 2 y 5 minutos ⏳`,
    progress: (found, total) => `⏳ Progreso: ${found}/${total} piezas encontradas...`,
    done: (found, total, savings) => `✅ Listo — encontré precios para ${found}/${total} piezas.${savings ? ` Ahorro potencial: *RD$${savings.toLocaleString()}*` : ''}`,
    error: (msg) => `Uy, algo salió mal 😬 ${msg}. Intenta de nuevo.`,
    ocr_error: 'No pude leer la imagen 😬 ¿Puedes mandar una foto más clara o un PDF?',
    timeout: '⏰ La sesión expiró por inactividad. Mándame la cotización cuando estés listo.',
    only_photos: 'Solo acepto fotos (JPG/PNG) o PDFs. Mándame la imagen de la cotización 📸',
    partial: (found, total) => `⚠️ Encontré precios para ${found}/${total} piezas. Las que faltan necesitan búsqueda manual.`,
  },
  en: {
    welcome: '👋 Hi! I\'m the parts bot. Send a photo or PDF of the supplier quote and I\'ll search for international prices.',
    send_photo: '📸 Send a photo or PDF of the supplier quote to get started.',
    not_allowed: '⛔ Your number is not authorized to use this bot.',
    processing: '⏳ Processing image... this takes a few seconds.',
    confirm_prompt: '✅ Reply OK to search prices\n✏️ Or send corrections',
    searching: (n) => `🔍 Searching ${n} parts... this takes 2-5 minutes.`,
    progress: (found, total) => `⏳ Progress: ${found}/${total} parts found...`,
    done: (found, total, savings) => `✅ Done! Found prices for ${found}/${total} parts.${savings ? ` Potential savings: RD$${savings.toLocaleString()}` : ''}`,
    error: (msg) => `❌ Error: ${msg}. Try again.`,
    ocr_error: '❌ Could not read the image. Try a clearer photo or a PDF.',
    timeout: '⏰ Session expired due to inactivity. Send a new image to start.',
    only_photos: '📸 I only accept photos (JPG/PNG) or PDF files. Send an image of the quote.',
    partial: (found, total) => `⚠️ Only found prices for ${found}/${total} parts. Missing ones need manual search.`,
  },
};

function t(jid, key, ...args) {
  const session = getSession(jid);
  const lang = session.lang || 'es';
  const val = MSG[lang][key];
  return typeof val === 'function' ? val(...args) : val;
}

// ─── WhatsApp Helpers ───────────────────────────────────────────────────────

// LID→phone mapping (populated at runtime via sock.store or manual mapping)
const LID_MAP = new Map();

function isAllowed(jid) {
  if (ALLOWED_NUMBERS.length === 0) return true;

  // LID format (@lid) — Baileys v6 uses these instead of phone JIDs.
  // Since this bot runs on a dedicated private number, only known contacts
  // can message it. Allow all LIDs and log them for auditing.
  if (jid.includes('@lid')) {
    consoleLog(`Auth: LID user ${jid} — allowed (private number)`);
    return true;
  }

  const num = jid.replace(/[^0-9]/g, '').replace(/@.*/, '');
  const last10 = num.slice(-10);
  const allowed = ALLOWED_NUMBERS.some(a => {
    const aLast10 = a.slice(-10);
    return last10 === aLast10 || num.includes(a) || a.includes(num);
  });
  consoleLog(`Auth check: jid=${jid} num=${num} allowed=${allowed}`);
  return allowed;
}

async function sendText(sock, jid, text) {
  try {
    await sock.sendMessage(jid, { text });
  } catch (e) {
    consoleLog('Send error:', e.message);
  }
}

async function sendDocument(sock, jid, filePath, fileName) {
  try {
    const buffer = fs.readFileSync(filePath);
    await sock.sendMessage(jid, {
      document: buffer,
      fileName: fileName,
      mimetype: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });
  } catch (e) {
    consoleLog('Send document error:', e.message);
  }
}

// ─── Python Bridge ──────────────────────────────────────────────────────────

function runPython(scriptPath, args = []) {
  return new Promise((resolve, reject) => {
    const proc = spawn(PYTHON, [scriptPath, ...args], {
      cwd: __dirname,
      env: { ...process.env, PYTHONPATH: __dirname },
      timeout: parseInt(process.env.SEARCH_TIMEOUT_SECONDS || '300') * 1000,
    });

    let stdout = '';
    let stderr = '';

    proc.stdout.on('data', d => { stdout += d.toString(); });
    proc.stderr.on('data', d => { stderr += d.toString(); });

    proc.on('close', code => {
      if (code === 0) {
        try {
          resolve(JSON.parse(stdout));
        } catch {
          resolve({ raw: stdout, error: null });
        }
      } else {
        reject(new Error(stderr || `Process exited with code ${code}`));
      }
    });

    proc.on('error', reject);
  });
}

// ─── Session Persistence ────────────────────────────────────────────────────

async function getNextSessionCode() {
  const { data, error } = await supabase
    .from('parts_sessions')
    .select('code')
    .order('created_at', { ascending: false })
    .limit(1);
  if (error || !data || data.length === 0) return 'S-0001';
  const lastNum = parseInt((data[0].code || 'S-0000').replace('S-', ''), 10) || 0;
  return `S-${String(lastNum + 1).padStart(4, '0')}`;
}

async function saveSessionToDb(code, jid, extractedData, results, excelFilename) {
  try {
    const vehicle = extractedData.vehicle || {};
    await supabase.from('parts_sessions').insert({
      code,
      phone_number: jid.replace(/[^0-9]/g, '').replace(/@.*/, ''),
      vehicle_vin: extractedData.vin || null,
      vehicle_year: vehicle.year || null,
      vehicle_make: vehicle.make || null,
      vehicle_model: vehicle.model || null,
      parts_list: extractedData.parts || [],
      results: results || [],
      supplier_total: extractedData.supplier_total_dop || null,
      excel_filename: excelFilename || null,
      status: 'active',
    });
    consoleLog(`Session ${code} saved to Supabase`);
  } catch (e) {
    consoleLog('saveSessionToDb error:', e.message);
  }
}

async function updateSessionStatus(code, status) {
  if (!code) return;
  try {
    const update = { status };
    if (status === 'closed') update.closed_at = new Date().toISOString();
    await supabase.from('parts_sessions').update(update).eq('code', code);
  } catch (e) {
    consoleLog('updateSessionStatus error:', e.message);
  }
}

async function loadSessionByCode(code) {
  try {
    const { data, error } = await supabase
      .from('parts_sessions')
      .select('*')
      .eq('code', code.toUpperCase())
      .single();
    if (error || !data) return null;
    return data;
  } catch { return null; }
}

async function findRecentSession(phoneNumber, vehicleHint) {
  try {
    let q = supabase
      .from('parts_sessions')
      .select('*')
      .eq('phone_number', phoneNumber.replace(/[^0-9]/g, '').replace(/@.*/, ''))
      .in('status', ['active', 'reviewing'])
      .order('created_at', { ascending: false })
      .limit(5);
    const { data, error } = await q;
    if (error || !data || data.length === 0) return null;
    if (vehicleHint) {
      const hint = vehicleHint.toLowerCase();
      const match = data.find(s =>
        (s.vehicle_make || '').toLowerCase().includes(hint) ||
        (s.vehicle_model || '').toLowerCase().includes(hint)
      );
      if (match) return match;
    }
    return data.length === 1 ? data[0] : data; // array = ambiguous
  } catch { return null; }
}

// ─── Correction Helpers ─────────────────────────────────────────────────────

/**
 * Bug 6: Sonnet-powered correction handler via Python subprocess.
 * Calls whatsapp/correction_handler.py which returns a structured action envelope.
 * Used as the fallback when legacy parseCorrection can't interpret the message.
 * Returns the envelope {action, params, explanation_es} or null on error.
 */
async function handleCorrectionPy(text, parts, vehicle, history) {
  return new Promise((resolve) => {
    const proc = spawn(PYTHON, [path.join(__dirname, 'whatsapp', 'correction_handler.py')], {
      cwd: __dirname,
      env: { ...process.env, PYTHONPATH: __dirname },
      timeout: 30000,
    });
    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', d => { stdout += d.toString(); });
    proc.stderr.on('data', d => { stderr += d.toString(); });
    proc.on('close', code => {
      if (code !== 0) {
        consoleLog('handleCorrectionPy exit', code, stderr.slice(-300));
        return resolve(null);
      }
      try {
        resolve(JSON.parse(stdout.trim()));
      } catch (e) {
        consoleLog('handleCorrectionPy parse error:', e.message, stdout.slice(0, 300));
        resolve(null);
      }
    });
    proc.on('error', (e) => {
      consoleLog('handleCorrectionPy spawn error:', e.message);
      resolve(null);
    });
    proc.stdin.write(JSON.stringify({
      parts: parts || [],
      vehicle: vehicle || {},
      history: history || [],
      message: text || '',
    }));
    proc.stdin.end();
  });
}

/**
 * Use Sonnet to parse a correction message against the current parts list.
 * Returns { part_index, corrected_name_english, corrected_name_dr, side, position }
 * or null if the message is not a correction.
 */
async function parseCorrection(text, parts) {
  const partsList = parts.map((p, i) =>
    `${i + 1}. ${p.name_original || p.name_dr || ''} → ${p.name_english || ''} (lado: ${p.side || 'ninguno'}, pos: ${p.position || 'ninguna'})`
  ).join('\n');

  const prompt = `Eres un asistente que interpreta mensajes de empleados de talleres de carrocería en República Dominicana.
Acaban de recibir los resultados de búsqueda de piezas de auto y están corrigiendo errores.

Lista de piezas buscadas:
${partsList}

Mensaje del usuario: "${text}"

VOCABULARIO DOMINICANO QUE DEBES ENTENDER:
- "el de alante" / "el del frente" / "delantero" / "de frente" → position: front
- "el de atrás" / "trasero" / "de atrás" → position: rear
- "el del lado del chofer" / "lado del conductor" / "izquierdo" / "izq" → side: left
- "el del pasajero" / "lado del pasajero" / "derecho" / "der" → side: right
- "eso no es" / "salió mal" / "está mal" / "no es eso" / "ese no" / "búscame es un" / "es un" / "debería ser" → indica que la pieza está mal
- "bonete" → hood
- "farol" / "faro" / "pantalla" → headlight
- "stop" / "violeta" → tail light
- "guardafango" / "aleta" → fender
- "bumper" / "defensa" → bumper cover
- "catre" → control arm
- "piña" → wheel hub/bearing
- "espejo" → side mirror
- "parrilla" → grille
- "cran" → oil pan
- "flear" → fender flare
- "guía de bumper" → bumper bracket/support

REGLAS:
1. Si el usuario menciona un número (#4, "el 4", "la cuatro", "número 4", "pieza 4") → ese es el part_index
2. Si NO menciona número pero describe una pieza (ej: "el bumper salió mal"), busca cuál pieza de la lista coincide mejor
3. Si menciona "el de alante" sin número, busca la pieza con position=front en la lista
4. Si menciona "el del chofer" sin número, busca la pieza con side=left en la lista
5. Si el mensaje parece ser un vehículo (marca + modelo + año, ej: "Toyota Camry 2018", "Porsche Macan 2016") o un VIN de 17 caracteres → es una corrección de vehículo
6. Patrones de corrección de vehículo: "el carro es un X", "es un X", "el vehículo es X", "eso es un X [año]", o simplemente "X [modelo] [año]"

Responde SOLO con JSON válido (sin markdown):

Si es una corrección de pieza:
{"is_correction": true, "is_vehicle_correction": false, "part_index": <número 1-based>, "corrected_name_english": "<nombre en inglés>", "corrected_name_dr": "<nombre en español DR o null>", "side": "<left|right|null>", "position": "<front|rear|null>"}

Si es una corrección de vehículo:
{"is_correction": false, "is_vehicle_correction": true, "vehicle": {"year": "<año o null>", "make": "<marca o null>", "model": "<modelo o null>"}, "vin": "<VIN de 17 caracteres o null>"}

Si es confirmación ("listo", "todo bien", "dale", "ok", "perfecto", "bien", "está bien", "looks good", "done", "gracias"):
{"is_correction": false, "is_vehicle_correction": false, "is_done": true}

Si no se entiende:
{"is_correction": false, "is_vehicle_correction": false, "is_done": false}`;

  try {
    const msg = await anthropic.messages.create({
      model: SONNET_MODEL,
      max_tokens: 256,
      system: 'Respond ONLY with valid JSON. No prose, no explanation, no markdown fences.',
      messages: [{ role: 'user', content: prompt }],
    });
    const raw = msg.content[0].text.trim();
    // Strip markdown fences if present
    let clean = raw.replace(/^```json?\s*/i, '').replace(/\s*```$/, '').trim();
    // If model added prose before/after JSON, extract the JSON object
    const jsonMatch = clean.match(/\{[\s\S]*\}/);
    if (jsonMatch) clean = jsonMatch[0];
    const result = JSON.parse(clean);
    return result;
  } catch (e) {
    consoleLog('parseCorrection error:', e.message);
    return null;
  }
}

// ─── Conversational Chat Layer ──────────────────────────────────────────────

const CHAT_SYSTEM_PROMPT = `Eres Pieza Finder, un asistente de taller en República Dominicana.
Ayudas a cotizar piezas de carros — el usuario te manda una foto o lista de piezas con el vehículo y tú consigues los mejores precios buscando en eBay y suplidores en EEUU.

Habla como un dominicano normal en WhatsApp. Informal, directo, tutea al usuario.
Respuestas cortas a menos que el usuario necesite más detalle.
Si el usuario hace una pregunta, respóndela. Si quiere conversar, conversa.
Cuando sea natural, guía hacia lo que necesitas: vehículo + lista de piezas.
Nunca repitas el mismo mensaje dos veces seguidas.
No uses listas ni bullets a menos que sea necesario — esto es WhatsApp.`;

async function handleChat(jid, userMessage) {
  appendHistory(jid, 'user', userMessage);
  const history = getHistory(jid);
  const response = await anthropic.messages.create({
    model: SONNET_MODEL,
    max_tokens: 1024,
    system: CHAT_SYSTEM_PROMPT,
    messages: history,
  });
  const reply = response.content[0].text;
  appendHistory(jid, 'assistant', reply);
  return reply;
}

/**
 * Log a correction — insert new or increment times_seen on existing record.
 * At 3+ occurrences, marks auto_promoted and updates translation cache via Python.
 */
async function logCorrection(vehicle, originalPart, correctedPart, correctionMessage, partIndex) {
  const make = (vehicle.make || '').trim();
  const model = (vehicle.model || '').trim();
  const originalName = (originalPart.name_english || originalPart.name_original || '').trim();
  const correctedName = (correctedPart.name_english || '').trim();
  if (!originalName || !correctedName) return;

  try {
    // Check for existing correction record
    const { data: existing } = await supabase
      .from('parts_corrections')
      .select('id, times_seen, auto_promoted')
      .ilike('vehicle_make', make)
      .ilike('vehicle_model', model)
      .ilike('part_name_original', originalName)
      .ilike('part_name_corrected', correctedName)
      .limit(1);

    if (existing && existing.length > 0) {
      const row = existing[0];
      const newSeen = (row.times_seen || 1) + 1;
      const confidence = newSeen >= 3 ? 'confirmed' : newSeen >= 2 ? 'likely' : 'suggested';
      const autoPromoted = newSeen >= 3;
      await supabase.from('parts_corrections')
        .update({ times_seen: newSeen, correction_confidence: confidence, auto_promoted: autoPromoted })
        .eq('id', row.id);
      consoleLog(`Correction incremented: '${originalName}' → '${correctedName}' (${newSeen}x, ${confidence})`);

      // Promote to translation cache when first confirmed
      if (autoPromoted && !row.auto_promoted) {
        consoleLog(`Auto-promoting '${originalName}' → '${correctedName}' to translation cache`);
        // Write to translation_cache.json on the server
        const cacheFile = path.join(__dirname, 'cache', 'translation_cache.json');
        try {
          let cache = {};
          if (fs.existsSync(cacheFile)) {
            cache = JSON.parse(fs.readFileSync(cacheFile, 'utf8'));
          }
          const key = originalName.toLowerCase().trim();
          if (!cache[key]) {
            cache[key] = correctedName;
            fs.mkdirSync(path.dirname(cacheFile), { recursive: true });
            fs.writeFileSync(cacheFile, JSON.stringify(cache, null, 2));
          }
        } catch (e2) {
          consoleLog('Translation cache write error:', e2.message);
        }
      }
    } else {
      await supabase.from('parts_corrections').insert({
        vehicle_year: vehicle.year || null,
        vehicle_make: make,
        vehicle_model: model,
        vin: vehicle.vin || null,
        part_index: partIndex,
        part_name_original: originalName,
        part_name_corrected: correctedName,
        side_original: originalPart.side || null,
        side_corrected: correctedPart.side || null,
        position_original: originalPart.position || null,
        position_corrected: correctedPart.position || null,
        correction_message: correctionMessage,
        times_seen: 1,
        correction_confidence: 'suggested',
        auto_promoted: false,
      });
      consoleLog(`New correction: '${originalName}' → '${correctedName}'`);
    }
  } catch (e) {
    consoleLog('logCorrection error:', e.message);
  }
}

// ─── Media Queue ────────────────────────────────────────────────────────────

/**
 * Process a single already-downloaded media file through OCR → confirmation.
 * Called by processNextQueued after popping from the queue.
 */
async function processSingleMedia(sock, jid, mediaPath, ext) {
  const session = getSession(jid);
  if (session.timeout) clearTimeout(session.timeout);
  session.state = 'processing';
  session.extractedData = null;
  session.timeout = null;
  session.lang = session.lang || 'es';

  const queueRemaining = getMediaQueue(jid).length;
  if (queueRemaining > 0) {
    await sendText(sock, jid, session.lang === 'es'
      ? `⏳ Procesando cotización... quedan *${queueRemaining}* más en cola.`
      : `⏳ Processing quote... *${queueRemaining}* more in queue.`);
  } else {
    await sendText(sock, jid, t(jid, 'processing'));
  }

  try {
    const extractResult = await runPython(
      path.join(__dirname, 'search', 'run_ocr.py'),
      ['--input', mediaPath]
    );

    try { fs.unlinkSync(mediaPath); } catch { }

    if (extractResult.error) {
      await sendText(sock, jid, t(jid, 'ocr_error'));
      resetSession(jid);
      return;
    }

    if (!extractResult.parts || extractResult.parts.length === 0) {
      await sendText(sock, jid, t(jid, 'ocr_error'));
      resetSession(jid);
      return;
    }

    // Merge pendingVehicleContext (VIN/vehicle text sent alongside the image) into OCR result
    if (session.pendingVehicleContext) {
      const ctx = session.pendingVehicleContext;
      if (ctx.vin && !extractResult.vin) extractResult.vin = ctx.vin;
      if (ctx.vehicleText && extractResult.vehicle) {
        const v = extractResult.vehicle;
        // Only fill missing fields — don't overwrite what OCR already found
        if (!v.year) { const ym = ctx.vehicleText.match(/\b(19|20)\d{2}\b/); if (ym) v.year = ym[0]; }
        if (!v.make || !v.model) {
          // Let parse_text.py handle it — store raw text as hint on the extraction
          extractResult._vehicleHint = ctx.vehicleText;
        }
      }
      delete session.pendingVehicleContext;
    }

    session.extractedData = extractResult;
    session.state = 'awaiting_confirmation';

    const vehicle = extractResult.vehicle || {};
    const vin = extractResult.vin || 'N/A';

    // Inject context into chat history so conversation stays coherent mid-session
    const vehicleStr = `${vehicle.year || ''} ${vehicle.make || ''} ${vehicle.model || ''}`.trim();
    appendHistory(jid, 'assistant', `Procesé la cotización — ${vehicleStr || 'vehículo desconocido'}, ${extractResult.parts.length} piezas. Voy a confirmar con el usuario.`);

    let confirmMsg = `🚗 *Vehículo:* ${vehicle.year || '?'} ${vehicle.make || '?'} ${vehicle.model || '?'}\n`;
    confirmMsg += `🔑 *VIN:* ${vin}\n\n`;
    confirmMsg += `📋 *Piezas encontradas:*\n`;
    confirmMsg += '─────────────────────\n';

    extractResult.parts.forEach((p, i) => {
      const side = p.side ? ` (${p.side === 'left' ? 'Izq' : 'Der'})` : '';
      const pos = p.position ? ` ${p.position === 'front' ? 'Del' : 'Tras'}` : '';
      const price = p.local_price ? ` — RD$${p.local_price.toLocaleString()}` : '';
      const en = p.name_english ? ` → ${p.name_english}` : '';
      const qty = (p.quantity && p.quantity > 1) ? ` x${p.quantity}` : '';
      confirmMsg += `${i + 1}. ${p.name_original || p.name_dr}${side}${pos}${en}${qty}${price}\n`;
    });

    confirmMsg += '─────────────────────\n';
    confirmMsg += t(jid, 'confirm_prompt');
    confirmMsg += '\n✏️ _Para corregir cantidad envia p.ej: "2 bumper delantero"_';

    if (queueRemaining > 0) {
      confirmMsg += `\n\n_📋 ${queueRemaining} cotización${queueRemaining !== 1 ? 'es' : ''} más en cola_`;
    }

    await sendText(sock, jid, confirmMsg);

    session.timeout = setTimeout(async () => {
      if (session.state === 'awaiting_confirmation') {
        await sendText(sock, jid, t(jid, 'timeout'));
        resetSession(jid);
      }
    }, 30 * 60 * 1000);

  } catch (e) {
    consoleLog('Media processing error:', e.message);
    await sendText(sock, jid, t(jid, 'error', e.message));
    resetSession(jid);
  }
}

/**
 * Pop the next item off a JID's queue and start processing it.
 * No-ops if the session is busy or queue is empty.
 */
async function processNextQueued(sock, jid) {
  const queue = getMediaQueue(jid);
  if (queue.length === 0) return;
  const session = getSession(jid);
  if (session.state !== 'idle') return;
  const item = queue.shift();
  await processSingleMedia(sock, jid, item.mediaPath, item.ext);
}

// ─── Search Pipeline ────────────────────────────────────────────────────────

/**
 * Run the full search pipeline via Python subprocess.
 * This calls search/run_search.py which orchestrates everything.
 */
async function runSearchPipeline(sock, jid, extractedData) {
  const session = getSession(jid);
  session.state = 'searching';

  const totalParts = extractedData.parts.length;
  await sendText(sock, jid, t(jid, 'searching', totalParts));

  // Write extracted data to temp file for Python
  const ts = Date.now();
  const inputPath = path.join(OUTPUT_DIR, `input_${ts}.json`);
  const outputExcel = path.join(OUTPUT_DIR, `parts_${ts}.xlsx`);
  const resultsJsonPath = path.join(OUTPUT_DIR, `results_${ts}.json`);

  fs.writeFileSync(inputPath, JSON.stringify({
    extraction: extractedData,
    output_path: outputExcel,
  }));

  try {
    // Start progress updates
    let lastUpdate = 0;
    const progressInterval = setInterval(async () => {
      // Check if progress file exists (Python writes it)
      const progressPath = inputPath.replace('.json', '_progress.json');
      try {
        if (fs.existsSync(progressPath)) {
          const progress = JSON.parse(fs.readFileSync(progressPath, 'utf8'));
          if (progress.found > lastUpdate) {
            lastUpdate = progress.found;
            await sendText(sock, jid, t(jid, 'progress', progress.found, totalParts));
          }
        }
      } catch { /* ignore progress read errors */ }
    }, 15000); // Check every 15s

    // Run Python search
    const result = await runPython(
      path.join(__dirname, 'search', 'run_search.py'),
      ['--input', inputPath, '--output', outputExcel, '--results-output', resultsJsonPath]
    );

    clearInterval(progressInterval);

    // Send results
    if (result.error) {
      await sendText(sock, jid, t(jid, 'error', result.error));
      resetSession(jid);
      return;
    }

    if (fs.existsSync(outputExcel)) {
      const summary = result.summary || {};
      const found = summary.found || 0;
      const total = summary.total_parts || totalParts;
      const savings = summary.total_savings_dop || 0;
      const flags = result.sonnet_flags || [];

      // Send Sonnet warning BEFORE the Excel if there are flagged issues
      if (flags.length > 0) {
        const flagLines = flags.slice(0, 10).join('\n');
        const warnMsg = `⚠️ ${flags.length} posible${flags.length > 1 ? 's' : ''} problema${flags.length > 1 ? 's' : ''} encontrado${flags.length > 1 ? 's' : ''}:\n${flagLines}\n\nVerifica antes de ordenar.`;
        await sendText(sock, jid, warnMsg);
      }

      // Send Excel
      const vehicle = extractedData.vehicle || {};
      const fileName = `Piezas_${vehicle.year || ''}_${vehicle.make || ''}_${vehicle.model || ''}_${new Date().toISOString().slice(0, 10)}.xlsx`;
      await sendDocument(sock, jid, outputExcel, fileName);

      // Send summary message
      if (found === total) {
        await sendText(sock, jid, t(jid, 'done', found, total, savings > 0 ? Math.round(savings) : null));
      } else {
        await sendText(sock, jid, t(jid, 'partial', found, total));
      }

      // Transition to reviewing state if results JSON was saved
      if (fs.existsSync(resultsJsonPath)) {
        const session2 = getSession(jid);
        const sessionCode = await getNextSessionCode();

        session2.state = 'reviewing';
        session2.reviewResultsPath = resultsJsonPath;
        session2.reviewExcelPath = outputExcel;
        session2.reviewVehicle = extractedData.vehicle || {};
        session2.reviewExtraction = extractedData;
        session2.reviewSupplierTotal = extractedData.supplier_total_dop || null;
        session2.reviewSessionCode = sessionCode;

        // Persist session to Supabase
        const resultsData = JSON.parse(fs.readFileSync(resultsJsonPath, 'utf8'));
        await saveSessionToDb(sessionCode, jid, extractedData, resultsData.results, fileName);

        const numParts = (extractedData.parts || []).length;
        await sendText(sock, jid, session2.lang === 'es'
          ? `📋 Sesión *${sessionCode}* — ${numParts} pieza${numParts !== 1 ? 's' : ''}.\n\nRevisa el Excel 👀 Si algo salió mal dime cuál y cómo debe ser. Ejemplo: _"el #4 salió mal, es un bumper cover"_.\n\nCuando esté todo bien, di *listo*. La sesión queda abierta para correcciones cuando quieras.`
          : `📋 Session *${sessionCode}* — ${numParts} part${numParts !== 1 ? 's' : ''}.\n\nCheck the Excel 👀 If anything looks wrong, tell me which part and what it should be.\n\nSay *done* when it all looks good. The session stays open for corrections.`);

        // No timeout — sessions can be corrected at any time via session code
      } else {
        resetSession(jid);
      }

    } else {
      await sendText(sock, jid, t(jid, 'error', 'No se genero el archivo Excel'));
      resetSession(jid);
    }

    // Cleanup input files (keep Excel and results JSON for review window)
    try { fs.unlinkSync(inputPath); } catch { }
    const progressPath = inputPath.replace('.json', '_progress.json');
    try { fs.unlinkSync(progressPath); } catch { }
    // Keep Excel + results JSON for 2 hours then delete
    setTimeout(() => {
      try { fs.unlinkSync(outputExcel); } catch { }
      try { fs.unlinkSync(resultsJsonPath); } catch { }
    }, 7200000);

  } catch (e) {
    consoleLog('Search pipeline error:', e.message);
    await sendText(sock, jid, t(jid, 'error', e.message));
    resetSession(jid);
  }
}

// ─── Message Handler ────────────────────────────────────────────────────────

async function handleMessage(sock, msg) {
  const jid = msg.key.remoteJid;
  if (!jid || jid === 'status@broadcast') return;
  if (msg.key.fromMe) return;

  // Whitelist check
  if (!isAllowed(jid)) {
    await sendText(sock, jid, MSG.es.not_allowed);
    return;
  }

  const session = getSession(jid);
  const message = msg.message;
  if (!message) return;

  // Detect language from text
  const textContent = message.conversation
    || message.extendedTextMessage?.text
    || message.imageMessage?.caption
    || '';

  // Always respond in Spanish regardless of input language
  session.lang = 'es';

  // ── DEBUG ROUTING LOG ──

  // ── AUTO-RESTORE: if session lost on restart, ask before reloading ──
  // Only for text messages — images/docs always start fresh
  const hasMedia = !!(message.imageMessage || (message.documentMessage && (message.documentMessage.mimetype || '').includes('pdf')));
  if (session.state === 'idle' && textContent && !hasMedia) {
    try {
      const phoneNum = jid.replace(/[^0-9]/g, '').replace(/@.*/, '');
      const { data: recentSessions } = await supabase
        .from('parts_sessions')
        .select('*')
        .eq('phone_number', phoneNum)
        .in('status', ['active', 'reviewing'])
        .gte('created_at', new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString())
        .order('created_at', { ascending: false })
        .limit(1);

      if (recentSessions && recentSessions.length === 1) {
        const dbSession = recentSessions[0];
        consoleLog(`Pending restore prompt for session ${dbSession.code} to ${jid}`);
        session.state = 'pending_restore';
        session.pendingRestoreData = dbSession;
        const v = dbSession;
        const vehicle = `${v.vehicle_year || ''} ${v.vehicle_make || ''} ${v.vehicle_model || ''}`.trim();
        const parts = (dbSession.parts_list || []).length;
        await sendText(sock, jid, session.lang === 'es'
          ? `Oye, tienes la cotización *${dbSession.code}* abierta — ${vehicle}, ${parts} pieza${parts !== 1 ? 's' : ''}. ¿Seguimos con esa o prefieres empezar con una nueva?`
          : `Hey, you've got session *${dbSession.code}* open — ${vehicle}, ${parts} part${parts !== 1 ? 's' : ''}. Want to keep working on that one, or start fresh?`);
        return;
      }
    } catch (e) {
      consoleLog('Auto-restore error:', e.message);
    }
  }

  // ── STATE: PENDING_RESTORE — waiting for yes/no on session resume ──
  if (session.state === 'pending_restore') {
    const dbSession = session.pendingRestoreData;
    const lower = textContent.toLowerCase().trim();
    const isYes = ['si', 'sí', 'yes', 'dale', 'esa', 'seguir', 'seguimos', 'continuar', 'esa misma', 'claro', 'ok', 'bueno'].some(w => lower === w || lower.startsWith(w + ' '));
    const isNo = /\b(no|nueva|nuevo|fresh|empezar|empecemos|start|nah|new)\b/.test(lower) || lower.includes('una nueva') || lower.includes('mejor no') || lower.includes('empecemos de nuevo');

    if (hasMedia) {
      // They sent an image — treat as "start fresh"
      resetSession(jid);
      // Fall through to image handler below
    } else if (isYes && dbSession) {
      session.state = 'reviewing';
      session.reviewSessionCode = dbSession.code;
      session.reviewVehicle = {
        vin: dbSession.vehicle_vin,
        year: dbSession.vehicle_year,
        make: dbSession.vehicle_make,
        model: dbSession.vehicle_model,
      };
      session.reviewExtraction = {
        vin: dbSession.vehicle_vin,
        vehicle: session.reviewVehicle,
        parts: dbSession.parts_list || [],
        supplier_total_dop: dbSession.supplier_total || null,
      };
      session.reviewSupplierTotal = dbSession.supplier_total || null;
      const ts = Date.now();
      const resumeResultsPath = path.join(OUTPUT_DIR, `results_restore_${ts}.json`);
      fs.writeFileSync(resumeResultsPath, JSON.stringify({
        vehicle: session.reviewVehicle,
        results: dbSession.results || [],
      }));
      session.reviewResultsPath = resumeResultsPath;
      session.reviewExcelPath = null;
      delete session.pendingRestoreData;
      setTimeout(() => { try { fs.unlinkSync(resumeResultsPath); } catch {} }, 7200000);
      const v = session.reviewVehicle;
      await sendText(sock, jid, session.lang === 'es'
        ? `Dale, retomando *${dbSession.code}* — ${v.year || ''} ${v.make || ''} ${v.model || ''}. ¿Cuál pieza quieres corregir?`
        : `Got it, picking up *${dbSession.code}* — ${v.year || ''} ${v.make || ''} ${v.model || ''}. Which part needs fixing?`);
      return;
    } else if (isNo) {
      resetSession(jid);
      await sendText(sock, jid, session.lang === 'es'
        ? 'Perfecto, cuando quieras mándame la nueva cotización 📋'
        : 'Got it, send me the new quote whenever you\'re ready 📋');
      return;
    } else if (textContent) {
      // Didn't understand — re-ask gently
      const v = dbSession;
      const vehicle = `${v.vehicle_year || ''} ${v.vehicle_make || ''} ${v.vehicle_model || ''}`.trim();
      await sendText(sock, jid, session.lang === 'es'
        ? `¿Seguimos con la *${dbSession.code}* del ${vehicle} o arrancamos con una nueva?`
        : `Do you want to continue with *${dbSession.code}* for the ${vehicle}, or start a new one?`);
      return;
    }
  }

  // ── STATE: AWAITING_VEHICLE — user needs to clarify vehicle after failed parse ──
  if (session.state === 'awaiting_vehicle' && textContent && !hasMedia) {
    try {
      const msg = await anthropic.messages.create({
        model: SONNET_MODEL,
        max_tokens: 200,
        system: `You parse vehicle identification messages for a DR auto parts bot.
The user is correcting a failed vehicle parse. Their message is one of:
- A vehicle name: "Porsche Macan 2016", "Toyota Hilux 2018 SR5"
- A VIN: 17-character alphanumeric
- Unclear

Return ONLY valid JSON, no other text:
{"type":"vehicle"|"vin"|"unclear","vehicle":{"year":"2016","make":"Porsche","model":"Macan"}|null,"vin":"string"|null}`,
        messages: [{ role: 'user', content: textContent }],
      });
      const raw = msg.content[0].text.trim();
      const jsonMatch = raw.match(/\{[\s\S]*\}/);
      const parsed = jsonMatch ? JSON.parse(jsonMatch[0]) : null;

      if (parsed && (parsed.type === 'vehicle' || parsed.type === 'vin') && (parsed.vehicle?.model || parsed.vin)) {
        // Re-run the parts parse with the clarified vehicle injected
        const combinedText = parsed.vehicle
          ? `${parsed.vehicle.year || ''} ${parsed.vehicle.make || ''} ${parsed.vehicle.model || ''}\n${session.pendingPartsText || ''}`
          : `VIN:${parsed.vin}\n${session.pendingPartsText || ''}`;
        session.state = 'processing';
        session.pendingPartsText = null;
        await sendText(sock, jid, '⏳ Procesando lista de piezas...');
        const extractResult = await runPython(
          path.join(__dirname, 'search', 'parse_text.py'),
          ['--text', combinedText]
        );
        if (!extractResult.error && extractResult.parts?.length > 0 && (extractResult.vehicle?.year || extractResult.vehicle?.model)) {
          session.extractedData = extractResult;
          session.state = 'awaiting_confirmation';
          const v = extractResult.vehicle || {};
          let confirmMsg = `🚗 *Vehículo:* ${v.year || '?'} ${v.make || '?'} ${v.model || '?'}\n\n📋 *Piezas (${extractResult.parts.length}):*\n`;
          confirmMsg += '─────────────────────\n';
          extractResult.parts.forEach((p, i) => {
            const side = p.side ? ` (${p.side === 'left' ? 'Izq' : 'Der'})` : '';
            const pos = p.position ? ` ${p.position === 'front' ? 'Del' : 'Tras'}` : '';
            const price = p.local_price ? ` — RD$${p.local_price.toLocaleString()}` : '';
            confirmMsg += `${i + 1}. ${p.name_original || p.name_dr}${side}${pos}${price}\n`;
          });
          confirmMsg += '─────────────────────\n';
          confirmMsg += t(jid, 'confirm_prompt');
          await sendText(sock, jid, confirmMsg);
        } else {
          resetSession(jid);
          await sendText(sock, jid, 'No pude armar la búsqueda 😬 Mándame la cotización de nuevo.');
        }
      } else {
        await sendText(sock, jid, session.lang === 'es'
          ? '¿Puedes escribirlo así? Ej: *Toyota Camry 2018* 🚗'
          : 'Could you write it like this? E.g.: *Toyota Camry 2018* 🚗');
      }
    } catch (e) {
      consoleLog('awaiting_vehicle error:', e.message);
      resetSession(jid);
      await sendText(sock, jid, 'Algo salió mal 😬 Mándame la cotización de nuevo.');
    }
    return;
  }

  // ── STATE: SEARCHING (ignore messages while searching) ──
  if (session.state === 'searching') {
    await sendText(sock, jid, session.lang === 'es'
      ? '⏳ Búsqueda en progreso, dame unos minutitos 🔍'
      : '⏳ Search in progress... give me a few minutes 🔍');
    return;
  }

  // ── STATE: REVIEWING (post-delivery correction flow) ──
  if (session.state === 'reviewing' && textContent) {
    const parsed = await parseCorrection(textContent, session.reviewExtraction.parts || []);

    if (!parsed) {
      await sendText(sock, jid, session.lang === 'es'
        ? 'No te entendí bien 😅 ¿Cuál número de pieza quieres corregir y cómo debe ser? Ejemplo: _"el #3 salió mal, es un guardafango derecho"_'
        : 'Not sure what you mean 😅 Which part number is wrong and what should it be? E.g.: _"#3 is wrong, it\'s a right fender"_');
      return;
    }

    if (parsed.is_done) {
      await sendText(sock, jid, session.lang === 'es'
        ? '¡Perfecto, todo listo! 👍'
        : 'Perfect, all done! 👍');
      // Mark session closed in Supabase and cache verified results
      await updateSessionStatus(session.reviewSessionCode, 'closed');
      // Trigger cache update for verified results (no corrections = verified)
      try {
        await runPython(
          path.join(__dirname, 'search', 'cache_verified.py'),
          [
            '--results-json', session.reviewResultsPath,
            '--vehicle-make', (session.reviewVehicle.make || ''),
            '--vehicle-model', (session.reviewVehicle.model || ''),
            '--vehicle-year', String(session.reviewVehicle.year || 0),
          ]
        );
      } catch { /* non-critical */ }
      resetSession(jid);
      return;
    }

    if (!parsed.is_correction) {
      await sendText(sock, jid, session.lang === 'es'
        ? 'No te entendí bien 😅 Dime el número de la pieza y cómo debe ser, por ejemplo: _"el #2 salió mal, búscame un farol delantero derecho"_'
        : 'Not sure what you mean 😅 Tell me the part number and what it should be.');
      return;
    }

    const partIdx0 = (parsed.part_index || 1) - 1; // convert to 0-based
    const parts = session.reviewExtraction.parts || [];

    if (partIdx0 < 0 || partIdx0 >= parts.length) {
      await sendText(sock, jid, session.lang === 'es'
        ? `❓ No existe la pieza #${parsed.part_index}. La busqueda tiene ${parts.length} piezas.`
        : `❓ Part #${parsed.part_index} doesn't exist. The search has ${parts.length} parts.`);
      return;
    }

    const originalPart = parts[partIdx0];
    await sendText(sock, jid, session.lang === 'es'
      ? `Dale, buscando el ${parsed.corrected_name_dr || parsed.corrected_name_english} para el #${parsed.part_index}... 🔍`
      : `Got it, searching for ${parsed.corrected_name_english} for #${parsed.part_index}... 🔍`);

    // Build corrected part dict
    const correctedPart = {
      ...originalPart,
      name_english: parsed.corrected_name_english,
      name_dr: parsed.corrected_name_dr || originalPart.name_dr,
      side: parsed.side !== undefined ? parsed.side : originalPart.side,
      position: parsed.position !== undefined ? parsed.position : originalPart.position,
    };

    try {
      // Re-search just this one part
      const newResult = await runPython(
        path.join(__dirname, 'search', 'run_single_part.py'),
        [
          '--vehicle-json', JSON.stringify(session.reviewVehicle),
          '--part-json', JSON.stringify(correctedPart),
        ]
      );

      // Merge into stored results JSON
      const resultsData = JSON.parse(fs.readFileSync(session.reviewResultsPath, 'utf8'));
      resultsData.results[partIdx0] = newResult;
      fs.writeFileSync(session.reviewResultsPath, JSON.stringify(resultsData, null, 2));

      // Regenerate Excel
      const newExcelPath = path.join(OUTPUT_DIR, `parts_rev_${Date.now()}.xlsx`);
      await runPython(
        path.join(__dirname, 'search', 'regen_excel.py'),
        [
          '--results-json', session.reviewResultsPath,
          '--output', newExcelPath,
          ...(session.reviewSupplierTotal ? ['--supplier-total', String(session.reviewSupplierTotal)] : []),
        ]
      );

      // Delete old Excel, update session
      try { fs.unlinkSync(session.reviewExcelPath); } catch { }
      session.reviewExcelPath = newExcelPath;

      // Update extraction parts for future corrections
      session.reviewExtraction.parts[partIdx0] = correctedPart;

      // Send updated Excel
      const v = session.reviewVehicle || {};
      const fileName = `Piezas_${v.year || ''}_${v.make || ''}_${v.model || ''}_rev_${new Date().toISOString().slice(0, 10)}.xlsx`;
      await sendDocument(sock, jid, newExcelPath, fileName);

      const bestPrice = newResult.best_option ? `$${newResult.best_option.price?.toFixed(2)}` : null;
      const priceMsg = bestPrice ? ` El mejor precio que encontré es *${bestPrice}* 💪` : ' No encontré precio, puede necesitar búsqueda manual.';
      await sendText(sock, jid, session.lang === 'es'
        ? `Listo, actualicé el #${parsed.part_index} con el ${parsed.corrected_name_dr || parsed.corrected_name_english}.${priceMsg}\n\n¿Hay algo más que corregir? Si está todo bien, di *listo*.`
        : `Done, updated #${parsed.part_index} with ${parsed.corrected_name_english}.${bestPrice ? ` Best price found: *${bestPrice}*` : ' No price found, may need manual search.'}\n\nAnything else to fix? Say *done* when it all looks good.`);

      // Log correction to Supabase
      await logCorrection(
        session.reviewVehicle,
        originalPart,
        correctedPart,
        textContent,
        parsed.part_index
      );

      // Schedule cleanup of new Excel (2h from now)
      setTimeout(() => {
        try { fs.unlinkSync(newExcelPath); } catch { }
      }, 7200000);

    } catch (e) {
      consoleLog('Correction search error:', e.message);
      await sendText(sock, jid, session.lang === 'es'
        ? `Uy, hubo un problema buscando esa pieza 😬 Intenta de nuevo o di *listo* si quieres salir.`
        : `Oops, had a problem searching that part 😬 Try again or say *done* to exit.`);
    }
    return;
  }

  // ── Check for image/document ──
  const imageMsg = message.imageMessage;
  const docMsg = message.documentMessage;
  const hasImage = !!imageMsg;
  const hasPdf = docMsg && (docMsg.mimetype || '').includes('pdf');

  if (hasImage || hasPdf) {
    const ext = hasImage ? '.jpg' : '.pdf';
    const num = jid.replace(/[^0-9]/g, '').replace(/@.*/, '');

    // Download immediately — media buffers expire; queue the local file path instead
    let mediaPath;
    try {
      const buffer = await downloadMediaMessage(msg, 'buffer', {});
      mediaPath = path.join(OUTPUT_DIR, `quote_${Date.now()}${ext}`);
      fs.writeFileSync(mediaPath, buffer);
    } catch (e) {
      consoleLog('Media download error:', e.message);
      await sendText(sock, jid, t(jid, 'error', e.message));
      return;
    }

    const queue = getMediaQueue(jid);
    queue.push({ mediaPath, ext });
    imageTimestamps.set(num, Date.now()); // stamp for VIN pre-check in text buffer

    // Debounce 2.5 s — collect rapid back-to-back PDFs before acknowledging
    if (queueDebounce.has(num)) clearTimeout(queueDebounce.get(num));
    queueDebounce.set(num, setTimeout(async () => {
      queueDebounce.delete(num);
      const count = getMediaQueue(jid).length;
      const sess = getSession(jid);

      if (sess.state === 'idle') {
        if (count > 1) {
          await sendText(sock, jid, sess.lang === 'es'
            ? `📥 Recibí *${count} cotizaciones*, procesando una por una 👀`
            : `📥 Got *${count} quotes*, processing one at a time 👀`);
        }
        await processNextQueued(sock, jid);
      } else {
        // Session busy — acknowledge receipt, will auto-start after current flow ends
        await sendText(sock, jid, sess.lang === 'es'
          ? `📥 La${count > 1 ? 's' : ''} recibí — la${count > 1 ? 's' : ''} proceso en cuanto termine con esta 👌`
          : `📥 ${count > 1 ? `*${count} quotes* received` : 'Quote received'} — will process when current search is done 🕐`);
      }
    }, 2500));

    return;
  }

  // ── STATE: AWAITING_CONFIRMATION ──
  if (session.state === 'awaiting_confirmation' && textContent) {
    const lower = textContent.toLowerCase().trim();
    const confirmWords = ['ok', 'si', 'sí', 'yes', 'dale', 'confirmar', 'buscar',
      'confirmo', 'adelante', 'go', 'search', 'listo'];

    if (confirmWords.some(w => lower === w || lower.startsWith(w + ' '))) {
      // User confirmed — start search
      await runSearchPipeline(sock, jid, session.extractedData);
    } else {
      // User sent a correction — use Sonnet to parse with full list context
      await sendText(sock, jid, session.lang === 'es'
        ? '✏️ Aplicando correcciones...'
        : '✏️ Applying corrections...');

      try {
        const parsed = await parseCorrection(textContent, session.extractedData.parts || []);

        if (!parsed || (!parsed.is_correction && !parsed.is_vehicle_correction && !parsed.is_done)) {
          // Bug 6: fallback to Python Sonnet correction_handler which supports
          // richer actions (ask_clarification, add_part, remove_part, update_quantity, etc.)
          const env = await handleCorrectionPy(
            textContent,
            session.extractedData.parts || [],
            session.extractedData.vehicle || {},
            getHistory(jid) || [],
          );
          if (env && env.action) {
            const action = env.action;
            const params = env.params || {};
            const explain = env.explanation_es || '';

            if (action === 'confirm_all') {
              await runSearchPipeline(sock, jid, session.extractedData);
              return;
            }
            if (action === 'ask_clarification') {
              const q = params.question_es || explain ||
                (session.lang === 'es'
                  ? 'No te entendí 😅 ¿Puedes aclarar?'
                  : "Not sure what you mean 😅 Can you clarify?");
              await sendText(sock, jid, q);
              return;
            }
            if (action === 'out_of_scope') {
              await sendText(sock, jid, explain ||
                (session.lang === 'es'
                  ? 'No es una corrección. Responde *OK* para buscar o indícame qué ajustar.'
                  : 'Not a correction. Reply *OK* to search or tell me what to adjust.'));
              return;
            }
            if (action === 'update_quantity') {
              const idx0 = (params.index || 1) - 1;
              const p = session.extractedData.parts || [];
              if (idx0 >= 0 && idx0 < p.length && typeof params.quantity === 'number') {
                p[idx0].quantity = params.quantity;
                await sendText(sock, jid, explain || `✅ Actualizado #${idx0 + 1} x${params.quantity}`);
                return;
              }
            }
            if (action === 'remove_part') {
              const idx0 = (params.index || 1) - 1;
              const p = session.extractedData.parts || [];
              if (idx0 >= 0 && idx0 < p.length) {
                p.splice(idx0, 1);
                await sendText(sock, jid, explain || `✅ Pieza #${idx0 + 1} eliminada`);
                return;
              }
            }
            if (action === 'add_part') {
              const np = {
                name_original: params.name_dr || '',
                name_dr: params.name_dr || '',
                name_english: '',
                side: params.side || null,
                position: params.position || null,
                quantity: params.quantity || 1,
              };
              (session.extractedData.parts || []).push(np);
              await sendText(sock, jid, explain || `✅ Añadida: ${np.name_original}`);
              return;
            }
            if (action === 'rename_part' || action === 'fix_translation') {
              const idx0 = (params.index || 1) - 1;
              const p = session.extractedData.parts || [];
              if (idx0 >= 0 && idx0 < p.length) {
                if (params.name_dr) {
                  p[idx0].name_dr = params.name_dr;
                  p[idx0].name_original = params.name_dr;
                }
                if (params.name_english) p[idx0].name_english = params.name_english;
                await sendText(sock, jid, explain || `✅ Pieza #${idx0 + 1} actualizada`);
                return;
              }
            }
            if (action === 're_extract_metadata') {
              if (params.vehicle) {
                session.extractedData.vehicle = {
                  ...session.extractedData.vehicle,
                  ...params.vehicle,
                };
              }
              if (params.vin) session.extractedData.vin = params.vin;
              await sendText(sock, jid, explain || '✅ Vehículo actualizado');
              return;
            }
            if (action === 're_extract_parts') {
              await sendText(sock, jid, explain ||
                'Entendido — vuelve a enviar el documento para re-procesarlo.');
              return;
            }
          }

          // Final fallback — unchanged canonical message
          await sendText(sock, jid, session.lang === 'es'
            ? 'No te entendí 😅 ¿Cuál número está mal y cómo debe ser? Ej: _el #3 es un guardafango izquierdo_'
            : 'Not sure what you mean 😅 Which number is wrong and what should it be? E.g.: _#3 is a left fender_');
          return;
        }

        if (parsed.is_done) {
          // They confirmed mid-correction flow — proceed to search
          await runSearchPipeline(sock, jid, session.extractedData);
          return;
        }

        if (parsed.is_vehicle_correction) {
          // User corrected the vehicle — update session and re-display parts list
          if (parsed.vehicle) session.extractedData.vehicle = { ...session.extractedData.vehicle, ...parsed.vehicle };
          if (parsed.vin) session.extractedData.vin = parsed.vin;
          const v = session.extractedData.vehicle || {};
          let confirmMsg = `✅ *Vehículo actualizado:* ${v.year || '?'} ${v.make || '?'} ${v.model || '?'}\n`;
          if (session.extractedData.vin) confirmMsg += `🔑 *VIN:* ${session.extractedData.vin}\n`;
          confirmMsg += `\n📋 *Piezas:*\n`;
          confirmMsg += '─────────────────────\n';
          session.extractedData.parts.forEach((p, i) => {
            const side = p.side ? ` (${p.side === 'left' ? 'Izq' : 'Der'})` : '';
            const pos = p.position ? ` ${p.position === 'front' ? 'Del' : 'Tras'}` : '';
            const en = p.name_english ? ` → ${p.name_english}` : '';
            const qty = (p.quantity && p.quantity > 1) ? ` x${p.quantity}` : '';
            const price = p.local_price ? ` — RD$${p.local_price.toLocaleString()}` : '';
            confirmMsg += `${i + 1}. ${p.name_original || p.name_dr}${side}${pos}${en}${qty}${price}\n`;
          });
          confirmMsg += '─────────────────────\n';
          confirmMsg += t(jid, 'confirm_prompt');
          await sendText(sock, jid, confirmMsg);
          return;
        }

        // Apply correction by index (1-based from Sonnet → 0-based)
        const partIdx0 = (parsed.part_index || 1) - 1;
        const parts = session.extractedData.parts || [];

        if (partIdx0 < 0 || partIdx0 >= parts.length) {
          await sendText(sock, jid, session.lang === 'es'
            ? `❓ No existe la pieza #${parsed.part_index}. La lista tiene ${parts.length} piezas.`
            : `❓ Part #${parsed.part_index} doesn't exist. The list has ${parts.length} parts.`);
          return;
        }

        const originalPart = parts[partIdx0];
        session.extractedData.parts[partIdx0] = {
          ...originalPart,
          name_english: parsed.corrected_name_english,
          name_dr: parsed.corrected_name_dr || originalPart.name_dr,
          name_original: parsed.corrected_name_dr || originalPart.name_original,
          side: (parsed.side !== undefined && parsed.side !== null) ? parsed.side : originalPart.side,
          position: (parsed.position !== undefined && parsed.position !== null) ? parsed.position : originalPart.position,
        };

        // Re-show confirmation with updated list
        const vehicle = session.extractedData.vehicle || {};
        let confirmMsg = `🚗 *Vehículo:* ${vehicle.year || '?'} ${vehicle.make || '?'} ${vehicle.model || '?'}\n\n`;
        confirmMsg += `📋 *Piezas actualizadas:*\n`;
        confirmMsg += '─────────────────────\n';

        session.extractedData.parts.forEach((p, i) => {
          const side = p.side ? ` (${p.side === 'left' ? 'Izq' : 'Der'})` : '';
          const pos = p.position ? ` ${p.position === 'front' ? 'Del' : 'Tras'}` : '';
          const en = p.name_english ? ` → ${p.name_english}` : '';
          const qty = (p.quantity && p.quantity > 1) ? ` x${p.quantity}` : '';
          const price = p.local_price ? ` — RD$${p.local_price.toLocaleString()}` : '';
          confirmMsg += `${i + 1}. ${p.name_original || p.name_dr}${side}${pos}${en}${qty}${price}\n`;
        });

        confirmMsg += '─────────────────────\n';
        confirmMsg += t(jid, 'confirm_prompt');

        await sendText(sock, jid, confirmMsg);

      } catch (e) {
        consoleLog('Correction parse error:', e.message);
        await sendText(sock, jid, session.lang === 'es'
          ? '❌ Error al procesar la correccion. Responde *OK* para buscar con la lista actual.'
          : '❌ Error processing correction. Reply *OK* to search with current list.');
      }
    }
    return;
  }

  // ── STATE: IDLE — text message ──
  if (textContent) {
    const lower = textContent.toLowerCase().trim();

    // Session resumption — "S-0047 el #4" or "el Tucson de ayer"
    const sessionCodeMatch = textContent.match(/\bS-(\d{3,4})\b/i);
    const looksLikeResume = sessionCodeMatch ||
      lower.includes('ayer') || lower.includes('de ayer') ||
      lower.includes('sesion') || lower.includes('sesión') ||
      lower.includes('la de ayer') || lower.includes('el de ayer');

    if (looksLikeResume) {
      let dbSession = null;
      if (sessionCodeMatch) {
        dbSession = await loadSessionByCode(`S-${sessionCodeMatch[1].padStart(4, '0')}`);
      }
      if (!dbSession) {
        // Try to find by vehicle keyword or most recent
        const vehicleMatch = lower.match(/\b(tucson|santa fe|sportage|corolla|tacoma|camry|civic|sentra|forte|elantra|rav4|rogue|sonata|wrangler)\b/i);
        const hint = vehicleMatch ? vehicleMatch[1] : null;
        const phoneNum = jid.replace(/[^0-9]/g, '').replace(/@.*/, '');
        dbSession = await findRecentSession(phoneNum, hint);
      }

      if (!dbSession) {
        // No session found — fall through to normal message handling
      } else if (Array.isArray(dbSession)) {
        // Ambiguous — multiple sessions found
        const list = dbSession.slice(0, 3).map(s =>
          `• *${s.code}* — ${s.vehicle_year || '?'} ${s.vehicle_make || '?'} ${s.vehicle_model || '?'} (${new Date(s.created_at).toLocaleDateString('es-DO')})`
        ).join('\n');
        await sendText(sock, jid, `Encontré varias sesiones recientes. ¿Cuál es?\n\n${list}`);
        return;
      } else {
        // Single session found — reload into review state
        consoleLog(`Resuming session ${dbSession.code} for ${jid}`);
        const sess = getSession(jid);
        sess.state = 'reviewing';
        sess.reviewSessionCode = dbSession.code;
        sess.reviewVehicle = {
          vin: dbSession.vehicle_vin,
          year: dbSession.vehicle_year,
          make: dbSession.vehicle_make,
          model: dbSession.vehicle_model,
        };
        sess.reviewExtraction = {
          vin: dbSession.vehicle_vin,
          vehicle: sess.reviewVehicle,
          parts: dbSession.parts_list || [],
          supplier_total_dop: dbSession.supplier_total || null,
        };
        sess.reviewSupplierTotal = dbSession.supplier_total || null;
        // Restore results JSON from DB data to a temp file
        const ts = Date.now();
        const resumeResultsPath = path.join(OUTPUT_DIR, `results_resume_${ts}.json`);
        fs.writeFileSync(resumeResultsPath, JSON.stringify({
          vehicle: sess.reviewVehicle,
          results: dbSession.results || [],
        }));
        sess.reviewResultsPath = resumeResultsPath;
        sess.reviewExcelPath = null; // No Excel on disk, user has it
        setTimeout(() => { try { fs.unlinkSync(resumeResultsPath); } catch {} }, 7200000);

        const vehicle = sess.reviewVehicle;
        await sendText(sock, jid,
          `Dale, retomando sesión *${dbSession.code}* — ${vehicle.year || '?'} ${vehicle.make || '?'} ${vehicle.model || '?'} 📋\n\n¿Cuál pieza quieres corregir?`
        );

        // Process the rest of their message if it already contains a correction
        const correctionText = textContent.replace(/S-\d{3,4}/i, '').trim();
        if (correctionText.length > 3) {
          // Re-process as a correction by falling into reviewing state on next message
          await sendText(sock, jid, `Dime cuál número y cómo debe ser. Ejemplo: _"el #3 está mal, es un guardafango derecho"_`);
        }
        return;
      }
    }

    // Buffer text messages for 2.5s to handle fragmented forwards (VIN + vehicle name as separate msgs)
    const num = jid.replace(/[^0-9]/g, '').replace(/@.*/, '');
    if (!textBuffers.has(num)) textBuffers.set(num, { messages: [], timer: null });
    const textBuf = textBuffers.get(num);
    if (textBuf.timer) clearTimeout(textBuf.timer);
    textBuf.messages.push(textContent);
    textBuf.timer = setTimeout(async () => {
      const combined = textBuffers.get(num)?.messages.join('\n') || textContent;
      textBuffers.set(num, { messages: [], timer: null });


      // ── State guard: re-check state — it may have changed while buffer was collecting ──
      // e.g. OCR finished and state moved to awaiting_confirmation, or user is in reviewing
      if (session.state === 'awaiting_confirmation' || session.state === 'reviewing') {
        const activeParts = session.state === 'awaiting_confirmation'
          ? (session.extractedData?.parts || [])
          : (session.reviewExtraction?.parts || []);
        try {
          const parsed = await parseCorrection(combined, activeParts);
          if (!parsed || (!parsed.is_correction && !parsed.is_vehicle_correction && !parsed.is_done)) {
            const chatReply = await handleChat(jid, combined);
            await sendText(sock, jid, chatReply);
          } else if (parsed.is_done && session.state === 'awaiting_confirmation') {
            await runSearchPipeline(sock, jid, session.extractedData);
          } else if (parsed.is_done && session.state === 'reviewing') {
            await sendText(sock, jid, '¡Perfecto, todo listo! 👍');
            await updateSessionStatus(session.reviewSessionCode, 'closed');
            resetSession(jid);
          } else {
            // Re-enter the appropriate state handler by faking a message dispatch
            // Simplest: just call handleMessage recursively with the combined text
            const fakeMsg = { ...msg, message: { conversation: combined } };
            await handleMessage(sock, fakeMsg);
          }
        } catch (e) {
          consoleLog('Buffer state-guard error:', e.message);
          const chatReply = await handleChat(jid, combined).catch(() => '¿Me puedes mandar la cotización? Necesito el vehículo y las piezas 📋');
          await sendText(sock, jid, chatReply);
        }
        return;
      }

      const lines = combined.split(/[\n,]/).map(l => l.trim()).filter(Boolean);
      const lineCount = lines.length;
      const VIN_RE = /\b[A-HJ-NPR-Z0-9]{17}\b/i;
      const PART_KW_RE = /\b(bonete|farol|guardafango|bumper|catre|piña|violeta|stop|espejo|parrilla|bolsa|cinturon|frentil|clips|fender|hood|headlight|grille|control arm|brake|rotor|mirror|airbag|seatbelt)\b/i;
      const hasVehicleWord = /\b(20\d{2}|19\d{2}|hyundai|kia|toyota|honda|nissan|mazda|ford|chevy|chevrolet|dodge|jeep|mercedes|bmw|porsche|audi|sonata|tucson|elantra|santa fe|sportage|sorento|corolla|camry|civic|accord|sentra|altima|rav4|rogue|gle|wrangler|explorer|macan|veloster|hilux)\b/i.test(combined);
      const hasPartWord = PART_KW_RE.test(combined);
      const hasVIN = VIN_RE.test(combined);
      const lastImageTs = imageTimestamps.get(num) || 0;
      const batchHasImage = (Date.now() - lastImageTs) < (TEXT_BUFFER_MS + 1000);

      // ── VIN + image batch: store vehicle context for OCR pipeline ──
      if (hasVIN && batchHasImage && !hasPartWord) {
        const vinMatch = combined.match(/\b[A-HJ-NPR-Z0-9]{17}\b/i);
        const vehicleText = lines.filter(l => !VIN_RE.test(l)).join(' ').trim();
        session.pendingVehicleContext = {
          vin: vinMatch ? vinMatch[0].toUpperCase() : null,
          vehicleText: vehicleText || null,
        };
        return; // OCR pipeline will pick this up via pendingVehicleContext
      }

      // ── Only trigger the pipeline for an explicit parts list ──
      // Requires: VIN + 3+ lines with a part keyword  (forwarded cotización with VIN)
      //       OR: 4+ lines with vehicle word + part keyword (clear list, no VIN)
      // Everything else — including casual car mentions — goes to chat.
      const isExplicitPartsList = (hasVIN && lineCount >= 3 && hasPartWord)
        || (!hasVIN && lineCount >= 4 && hasPartWord && hasVehicleWord);

      if (!isExplicitPartsList) {
        try {
          const chatReply = await handleChat(jid, combined);
          await sendText(sock, jid, chatReply);
        } catch (e) {
          consoleLog('handleChat error:', e.message);
          await sendText(sock, jid, '¿Me puedes mandar la cotización? Necesito el vehículo y las piezas 📋');
        }
        return;
      }

      // Looks like a parts list — clear chat context and parse it
      clearHistory(jid);
      if (session.timeout) clearTimeout(session.timeout);
      session.state = 'processing';
      session.extractedData = null;
      session.timeout = null;

      await sendText(sock, jid, session.lang === 'es'
        ? '⏳ Procesando lista de piezas...'
        : '⏳ Processing parts list...');

      try {
        const extractResult = await runPython(
          path.join(__dirname, 'search', 'parse_text.py'),
          ['--text', combined]
        );

        if (extractResult.error || !extractResult.parts || extractResult.parts.length === 0) {
          await sendText(sock, jid, session.lang === 'es'
            ? 'No le vi piezas al mensaje 🤔 Mándame el vehículo y las piezas, ej: _Sonata 2018 / bonete / farol derecho_'
            : 'Couldn\'t find any parts in that message 🤔 Send me the vehicle and parts, e.g.: _Sonata 2018 / hood / right headlight_');
          resetSession(jid);
          return;
        }

        const vehicle = extractResult.vehicle || {};

        // Check we have enough vehicle info
        if (!vehicle.year || !vehicle.model) {
          session.state = 'awaiting_vehicle';
          session.pendingPartsText = combined;
          await sendText(sock, jid, session.lang === 'es'
            ? 'No reconocí el vehículo 🤔 ¿Cuál es? Ej: _Porsche Macan 2016_ o mándame el VIN 🔑'
            : 'Couldn\'t identify the vehicle 🤔 Which one is it? E.g.: _Porsche Macan 2016_ or send the VIN 🔑');
          return;
        }

        // Store and show confirmation
        session.extractedData = extractResult;
        session.state = 'awaiting_confirmation';

        // Inject context into chat history so conversation stays coherent mid-session
        const vehicleStrTxt = `${vehicle.year || ''} ${vehicle.make || ''} ${vehicle.model || ''}`.trim();
        appendHistory(jid, 'assistant', `Procesé la lista — ${vehicleStrTxt || 'vehículo desconocido'}, ${extractResult.parts.length} piezas. Voy a confirmar con el usuario.`);

        const vin = extractResult.vin || 'N/A';
        let confirmMsg = `🚗 *Vehículo:* ${vehicle.year || '?'} ${vehicle.make || '?'} ${vehicle.model || '?'}\n`;
        if (vin !== 'N/A') confirmMsg += `🔑 *VIN:* ${vin}\n`;
        confirmMsg += `\n📋 *Piezas:*\n`;
        confirmMsg += '─────────────────────\n';

        extractResult.parts.forEach((p, i) => {
          const side = p.side ? ` (${p.side === 'left' ? 'Izq' : 'Der'})` : '';
          const pos = p.position ? ` ${p.position === 'front' ? 'Del' : 'Tras'}` : '';
          const en = p.name_english ? ` → ${p.name_english}` : '';
          const qty = (p.quantity && p.quantity > 1) ? ` x${p.quantity}` : '';
          confirmMsg += `${i + 1}. ${p.name_original}${side}${pos}${en}${qty}\n`;
        });

        confirmMsg += '─────────────────────\n';
        confirmMsg += t(jid, 'confirm_prompt');

        await sendText(sock, jid, confirmMsg);

        session.timeout = setTimeout(async () => {
          if (session.state === 'awaiting_confirmation') {
            await sendText(sock, jid, t(jid, 'timeout'));
            resetSession(jid);
          }
        }, 30 * 60 * 1000);

      } catch (e) {
        consoleLog('Text parse error:', e.message);
        await sendText(sock, jid, t(jid, 'error', e.message));
        resetSession(jid);
      }
    }, TEXT_BUFFER_MS);
    return;
  }

  // ── Unsupported message type ──
  if (message.audioMessage || message.videoMessage || message.stickerMessage) {
    await sendText(sock, jid, t(jid, 'only_photos'));
  }
}

// ─── Weekly Report ──────────────────────────────────────────────────────────

let _globalSock = null; // Set when WhatsApp connects, used by weekly report

async function sendWeeklyReport() {
  if (!_globalSock) return;
  const matthewJid = `${(process.env.ALLOWED_NUMBERS || '').split(',')[0].replace(/[^0-9]/g, '')}@s.whatsapp.net`;
  if (!matthewJid || matthewJid === '@s.whatsapp.net') return;

  try {
    const oneWeekAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();

    const [{ count: searchCount }, { count: corrCount }, { data: corrections }, { data: vehicles }, { count: cacheCount }] = await Promise.all([
      supabase.from('parts_sessions').select('*', { count: 'exact', head: true }).gte('created_at', oneWeekAgo),
      supabase.from('parts_corrections').select('*', { count: 'exact', head: true }).gte('created_at', oneWeekAgo),
      supabase.from('parts_corrections').select('part_name_original,part_name_corrected,auto_promoted').eq('auto_promoted', true).gte('created_at', oneWeekAgo),
      supabase.from('parts_sessions').select('vehicle_make,vehicle_model').gte('created_at', oneWeekAgo),
      supabase.from('parts_cache').select('*', { count: 'exact', head: true }),
    ]);

    // Most searched vehicle
    const vehicleCounts = {};
    (vehicles || []).forEach(v => {
      const key = `${v.vehicle_make || '?'} ${v.vehicle_model || '?'}`;
      vehicleCounts[key] = (vehicleCounts[key] || 0) + 1;
    });
    const topVehicle = Object.entries(vehicleCounts).sort((a, b) => b[1] - a[1])[0];

    // Accuracy estimate: sessions without any correction / total sessions
    const { count: sessionsWithCorrections } = await supabase
      .from('parts_corrections')
      .select('*', { count: 'exact', head: true })
      .gte('created_at', oneWeekAgo);
    const sessTotal = searchCount || 0;
    const accuracyPct = sessTotal > 0
      ? Math.round(((sessTotal - Math.min(sessionsWithCorrections || 0, sessTotal)) / sessTotal) * 100)
      : 0;

    // New terms promoted to dictionary
    const newTerms = (corrections || []).map(c => `  • ${c.part_name_original} → ${c.part_name_corrected}`).join('\n') || '  Ninguno';

    const report = `📊 *Reporte semanal del buscador de piezas*\n` +
      `🔍 Búsquedas: ${searchCount || 0}\n` +
      `✏️ Correcciones: ${corrCount || 0}\n` +
      `📚 Términos nuevos al diccionario: ${(corrections || []).length}\n${newTerms}\n` +
      `🚗 Vehículo más buscado: ${topVehicle ? `${topVehicle[0]} (${topVehicle[1]}x)` : 'N/A'}\n` +
      `📈 Precisión sin corrección: ${accuracyPct}%\n` +
      `✅ Piezas verificadas en caché: ${cacheCount || 0}`;

    await sendText(_globalSock, matthewJid, report);
    consoleLog('Weekly report sent to Matthew');
  } catch (e) {
    consoleLog('Weekly report error:', e.message);
  }
}

function scheduleWeeklyReport() {
  const now = new Date();
  // AST = UTC-4. Monday 8AM AST = Monday 12:00 UTC
  const nextMonday = new Date(now);
  const day = now.getUTCDay(); // 0=Sun, 1=Mon
  const daysUntilMonday = day === 1 ? 7 : (8 - day) % 7;
  nextMonday.setUTCDate(now.getUTCDate() + daysUntilMonday);
  nextMonday.setUTCHours(12, 0, 0, 0);
  const msUntilFirst = nextMonday.getTime() - now.getTime();
  consoleLog(`Weekly report scheduled in ${Math.round(msUntilFirst / 3600000)}h`);
  setTimeout(() => {
    sendWeeklyReport();
    setInterval(sendWeeklyReport, 7 * 24 * 60 * 60 * 1000);
  }, msUntilFirst);
}

// ─── Bot Connection ─────────────────────────────────────────────────────────

async function startBot() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'silent' }),
    browser: ['Parts-Bot', 'Chrome', '120.0.0'],
    syncFullHistory: false,
  });

  // Handle connection events
  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      const QRCode = require('qrcode');
      const qrPath = path.join(__dirname, 'qr.png');
      QRCode.toFile(qrPath, qr, { width: 600, margin: 2 }, (err) => {
        if (err) consoleLog('QR save error:', err.message);
        else consoleLog(`✅ QR code saved to: ${qrPath} — open it and scan with WhatsApp`);
      });
    }

    if (connection === 'close') {
      const reason = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = reason !== DisconnectReason.loggedOut;
      consoleLog(`Connection closed. Reason: ${reason}. Reconnecting: ${shouldReconnect}`);

      if (shouldReconnect) {
        setTimeout(startBot, 5000);
      } else {
        consoleLog('Logged out. Delete auth_info/ and restart to re-authenticate.');
      }
    } else if (connection === 'open') {
      consoleLog('✅ Parts-Bot connected to WhatsApp');
      consoleLog(`📋 Allowed numbers: ${ALLOWED_NUMBERS.length > 0 ? ALLOWED_NUMBERS.join(', ') : 'ALL (no whitelist)'}`);
      _globalSock = sock;
    }
  });

  // Save auth credentials on update
  sock.ev.on('creds.update', saveCreds);

  // Handle incoming messages
  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;

    for (const msg of messages) {
      try {
        await handleMessage(sock, msg);
      } catch (e) {
        consoleLog('Message handler error:', e.message);
        logger.error({ err: e, msg: msg.key }, 'Message handler error');
      }
    }
  });
}

// ─── Start ──────────────────────────────────────────────────────────────────

consoleLog('🚀 Starting Parts-Bot on port', process.env.PORT || 3002);
scheduleWeeklyReport();
startBot().catch(e => {
  consoleLog('Fatal error:', e);
  process.exit(1);
});
