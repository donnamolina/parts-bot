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
    welcome: '👋 Hola! Soy el bot de piezas. Envia una foto o PDF de la cotizacion del suplidor y buscare precios internacionales.',
    send_photo: '📸 Envia una foto o PDF de la cotizacion del suplidor para comenzar.',
    not_allowed: '⛔ Tu numero no esta autorizado para usar este bot.',
    processing: '⏳ Procesando imagen... esto toma unos segundos.',
    confirm_prompt: '✅ Responde OK para buscar precios\n✏️ O envia correcciones',
    searching: (n) => `🔍 Buscando ${n} piezas... esto toma 2-5 minutos.`,
    progress: (found, total) => `⏳ Progreso: ${found}/${total} piezas encontradas...`,
    done: (found, total, savings) => `✅ Listo! Encontre precios para ${found}/${total} piezas.${savings ? ` Ahorro potencial: RD$${savings.toLocaleString()}` : ''}`,
    error: (msg) => `❌ Error: ${msg}. Intenta de nuevo.`,
    ocr_error: '❌ No pude leer la imagen. Intenta con una foto mas clara o un PDF.',
    timeout: '⏰ Sesion expirada por inactividad. Envia una nueva imagen para comenzar.',
    only_photos: '📸 Solo acepto fotos (JPG/PNG) o archivos PDF. Envia una imagen de la cotizacion.',
    partial: (found, total) => `⚠️ Solo encontre precios para ${found}/${total} piezas. Las que faltan necesitan busqueda manual.`,
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

// ─── Correction Helpers ─────────────────────────────────────────────────────

/**
 * Use Sonnet to parse a correction message against the current parts list.
 * Returns { part_index, corrected_name_english, corrected_name_dr, side, position }
 * or null if the message is not a correction.
 */
async function parseCorrection(text, parts) {
  const partsList = parts.map((p, i) =>
    `${i + 1}. ${p.name_original || p.name_dr || ''} → ${p.name_english || ''} (side: ${p.side || 'none'}, pos: ${p.position || 'none'})`
  ).join('\n');

  const prompt = `The user sent a correction message after receiving auto parts search results.

Current parts list:
${partsList}

User message: "${text}"

If this is a correction to a specific part, respond with ONLY valid JSON (no markdown):
{
  "is_correction": true,
  "part_index": <1-based number>,
  "corrected_name_english": "<english part name>",
  "corrected_name_dr": "<DR spanish name or null>",
  "side": "<left|right|null>",
  "position": "<front|rear|null>"
}

If this is NOT a correction (it's a confirmation like "ok", "todo bien", "listo", "looks good", "gracias"), respond with:
{"is_correction": false, "is_done": true}

If the message is unclear, respond with:
{"is_correction": false, "is_done": false}`;

  try {
    const msg = await anthropic.messages.create({
      model: 'claude-sonnet-4-6',
      max_tokens: 256,
      messages: [{ role: 'user', content: prompt }],
    });
    const raw = msg.content[0].text.trim();
    return JSON.parse(raw);
  } catch (e) {
    consoleLog('parseCorrection error:', e.message);
    return null;
  }
}

/**
 * Log a correction to the Supabase parts_corrections table.
 */
async function logCorrection(vehicle, originalPart, correctedPart, correctionMessage, partIndex) {
  try {
    await supabase.from('parts_corrections').insert({
      vehicle_year: vehicle.year || null,
      vehicle_make: vehicle.make || null,
      vehicle_model: vehicle.model || null,
      vin: vehicle.vin || null,
      part_index: partIndex,
      part_name_original: originalPart.name_english || originalPart.name_original || null,
      part_name_corrected: correctedPart.name_english || null,
      side_original: originalPart.side || null,
      side_corrected: correctedPart.side || null,
      position_original: originalPart.position || null,
      position_corrected: correctedPart.position || null,
      correction_message: correctionMessage,
    });
  } catch (e) {
    consoleLog('Supabase correction log error:', e.message);
  }
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
        session2.state = 'reviewing';
        session2.reviewResultsPath = resultsJsonPath;
        session2.reviewExcelPath = outputExcel;
        session2.reviewVehicle = extractedData.vehicle || {};
        session2.reviewExtraction = extractedData;
        session2.reviewSupplierTotal = extractedData.supplier_total_dop || null;

        await sendText(sock, jid, session2.lang === 'es'
          ? '🔍 *Revision activa* — Responde si algo esta mal, p.ej:\n_"el #4 esta mal, es un bumper cover delantero"_\nCuando este todo correcto, responde *listo*.'
          : '🔍 *Review active* — Reply if anything is wrong, e.g.:\n_"#4 is wrong, should be front bumper cover"_\nWhen everything looks good, reply *done*.');

        // Auto-exit review after 30 minutes
        session2.timeout = setTimeout(async () => {
          const s = getSession(jid);
          if (s.state === 'reviewing') {
            resetSession(jid);
          }
        }, 30 * 60 * 1000);
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

  if (textContent) {
    session.lang = detectLanguage(textContent);
  }
  if (!session.lang) session.lang = 'es';

  // ── STATE: SEARCHING (ignore messages while searching) ──
  if (session.state === 'searching') {
    await sendText(sock, jid, session.lang === 'es'
      ? '⏳ Busqueda en progreso... espera unos minutos.'
      : '⏳ Search in progress... wait a few minutes.');
    return;
  }

  // ── STATE: REVIEWING (post-delivery correction flow) ──
  if (session.state === 'reviewing' && textContent) {
    const parsed = await parseCorrection(textContent, session.reviewExtraction.parts || []);

    if (!parsed) {
      await sendText(sock, jid, session.lang === 'es'
        ? '❓ No entendi. Dime que pieza esta mal, p.ej: _"el #3 esta mal, es un guardafango derecho"_. O di *listo* para terminar.'
        : '❓ Not sure what you mean. Tell me which part is wrong, e.g.: _"#3 is wrong, it\'s a right fender"_. Or say *done* to finish.');
      return;
    }

    if (parsed.is_done) {
      await sendText(sock, jid, session.lang === 'es'
        ? '✅ Listo. Sesion de revision cerrada.'
        : '✅ Done. Review session closed.');
      resetSession(jid);
      return;
    }

    if (!parsed.is_correction) {
      await sendText(sock, jid, session.lang === 'es'
        ? '❓ No entendi la correccion. Dime el numero de pieza y el nombre correcto.'
        : '❓ Could not parse correction. Tell me the part number and correct name.');
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
      ? `🔄 Corrigiendo pieza #${parsed.part_index}: "${originalPart.name_original || originalPart.name_english}" → "${parsed.corrected_name_english}"...`
      : `🔄 Correcting part #${parsed.part_index}: "${originalPart.name_english}" → "${parsed.corrected_name_english}"...`);

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

      const bestPrice = newResult.best_option ? `$${newResult.best_option.price?.toFixed(2)}` : 'N/F';
      await sendText(sock, jid, session.lang === 'es'
        ? `✅ Pieza #${parsed.part_index} corregida. Mejor precio: ${bestPrice}\n\n¿Hay otro error? Di el numero o *listo* para terminar.`
        : `✅ Part #${parsed.part_index} corrected. Best price: ${bestPrice}\n\nAnother error? Tell me the number or say *done* to finish.`);

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
        ? `❌ Error buscando la pieza corregida: ${e.message}`
        : `❌ Error searching corrected part: ${e.message}`);
    }
    return;
  }

  // ── Check for image/document ──
  const imageMsg = message.imageMessage;
  const docMsg = message.documentMessage;
  const hasImage = !!imageMsg;
  const hasPdf = docMsg && (docMsg.mimetype || '').includes('pdf');

  if (hasImage || hasPdf) {
    // Cancel any pending timeout and reset to processing state
    // (Mutate the existing session object so state persists in the sessions map)
    if (session.timeout) clearTimeout(session.timeout);
    session.state = 'processing';
    session.extractedData = null;
    session.timeout = null;
    session.lang = session.lang || 'es';

    await sendText(sock, jid, t(jid, 'processing'));

    try {
      // Download media
      const buffer = await downloadMediaMessage(msg, 'buffer', {});
      const ext = hasImage ? '.jpg' : '.pdf';
      const mediaPath = path.join(OUTPUT_DIR, `quote_${Date.now()}${ext}`);
      fs.writeFileSync(mediaPath, buffer);

      // Run OCR extraction via Python
      const extractResult = await runPython(
        path.join(__dirname, 'search', 'run_ocr.py'),
        ['--input', mediaPath]
      );

      // Clean up media file
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

      // Store extraction and show confirmation
      session.extractedData = extractResult;
      session.state = 'awaiting_confirmation';

      // Build confirmation message
      const vehicle = extractResult.vehicle || {};
      const vin = extractResult.vin || 'N/A';
      let confirmMsg = `🚗 *Vehiculo:* ${vehicle.year || '?'} ${vehicle.make || '?'} ${vehicle.model || '?'}\n`;
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

      await sendText(sock, jid, confirmMsg);

      // Set 30-minute timeout
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
      // User sent a correction — re-parse and merge into the current parts list
      await sendText(sock, jid, session.lang === 'es'
        ? '✏️ Aplicando correcciones...'
        : '✏️ Applying corrections...');

      try {
        const corrResult = await runPython(
          path.join(__dirname, 'search', 'parse_text.py'),
          ['--text', textContent]
        );

        if (corrResult.error || !corrResult.parts || corrResult.parts.length === 0) {
          // Can't parse it — just re-show the current list with instructions
          await sendText(sock, jid, session.lang === 'es'
            ? '❓ No entendi la correccion. Responde *OK* para buscar, o envia el vehiculo + lista completa de piezas.\n\nEjemplo de cantidad:\n_2 bumper delantero_\n_3 farol derecho_'
            : '❓ Could not parse correction. Reply *OK* to search, or send vehicle + full parts list.\n\nFor quantities:\n_2 front bumper_\n_3 right headlight_');
          return;
        }

        const existing = session.extractedData;
        const corrParts = corrResult.parts;

        // If correction includes vehicle info, use it; otherwise keep original
        const vehicleInfo = (corrResult.vehicle && corrResult.vehicle.model)
          ? corrResult.vehicle
          : existing.vehicle;

        // Merge strategy:
        // 1. For each corrected part, find matching part in existing list by english name
        //    and update quantity. If no match, add it.
        // 2. Handle leading-number quantity shortcuts like "2 bumper" → qty=2 for bumper
        const mergedParts = [...existing.parts];

        for (const cp of corrParts) {
          const cpEn = (cp.name_english || '').toLowerCase();
          const cpSide = (cp.side || '').toLowerCase();

          // Find existing part with same english name (and matching side if specified)
          const idx = mergedParts.findIndex(ep => {
            const epEn = (ep.name_english || '').toLowerCase();
            const epSide = (ep.side || '').toLowerCase();
            const nameMatch = epEn === cpEn || epEn.includes(cpEn) || cpEn.includes(epEn);
            const sideMatch = !cpSide || !epSide || cpSide === epSide;
            return nameMatch && sideMatch;
          });

          if (idx >= 0) {
            // Update quantity (correction quantity takes precedence)
            mergedParts[idx] = { ...mergedParts[idx], quantity: cp.quantity || 1 };
          } else {
            // New part — add to list
            mergedParts.push(cp);
          }
        }

        // Update session
        session.extractedData = {
          ...existing,
          vehicle: vehicleInfo,
          parts: mergedParts,
        };

        // Re-show confirmation with updated list
        const vehicle = vehicleInfo || {};
        let confirmMsg = `🚗 *Vehiculo:* ${vehicle.year || '?'} ${vehicle.make || '?'} ${vehicle.model || '?'}\n\n`;
        confirmMsg += `📋 *Piezas actualizadas:*\n`;
        confirmMsg += '─────────────────────\n';

        mergedParts.forEach((p, i) => {
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

    // Greeting / help
    if (['hola', 'hi', 'hello', 'help', 'ayuda', 'inicio', 'start'].some(w => lower === w)) {
      await sendText(sock, jid, t(jid, 'welcome'));
      return;
    }

    // Check if this looks like a parts list (has multiple lines, or commas, or known part/vehicle words)
    const lineCount = textContent.split(/[\n,]/).filter(l => l.trim()).length;
    const hasVehicleWord = /\b(20\d{2}|19\d{2}|hyundai|kia|toyota|honda|nissan|mazda|ford|chevy|chevrolet|dodge|jeep|mercedes|bmw|sonata|tucson|elantra|santa fe|sportage|sorento|corolla|camry|civic|accord|sentra|altima|rav4|rogue|gle|wrangler|explorer)\b/i.test(lower);
    const hasPartWord = /\b(bonete|farol|guardafango|bumper|catre|piña|violeta|stop|espejo|parrilla|fender|hood|headlight|grille|control arm|brake|rotor|mirror)\b/i.test(lower);

    if (lineCount >= 2 || (hasVehicleWord && hasPartWord)) {
      // Looks like a parts list — parse it
      // Mutate existing session (don't resetSession — that detaches the reference)
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
          ['--text', textContent]
        );

        if (extractResult.error || !extractResult.parts || extractResult.parts.length === 0) {
          await sendText(sock, jid, session.lang === 'es'
            ? '❌ No pude identificar piezas en el mensaje. Formato:\n\nsonata 2018\nbonete\nfarol derecho\nbumper delantero'
            : '❌ Could not identify parts. Format:\n\nsonata 2018\nhood\nheadlight right\nfront bumper');
          resetSession(jid);
          return;
        }

        const vehicle = extractResult.vehicle || {};

        // Check we have enough vehicle info
        if (!vehicle.year || !vehicle.model) {
          await sendText(sock, jid, session.lang === 'es'
            ? '❓ No pude identificar el vehiculo. Incluye modelo y año, ejemplo:\n\nsonata 2018\nbonete\nfarol derecho'
            : '❓ Could not identify the vehicle. Include model and year, e.g.:\n\nsonata 2018\nhood\nheadlight right');
          resetSession(jid);
          return;
        }

        // Store and show confirmation
        session.extractedData = extractResult;
        session.state = 'awaiting_confirmation';

        const vin = extractResult.vin || 'N/A';
        let confirmMsg = `🚗 *Vehiculo:* ${vehicle.year || '?'} ${vehicle.make || '?'} ${vehicle.model || '?'}\n`;
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
        confirmMsg += '\n✏️ _Para corregir cantidad envia p.ej: "2 bumper delantero"_';

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
      return;
    }

    // Short text, no parts detected — show instructions
    await sendText(sock, jid, session.lang === 'es'
      ? '📸 Envia una foto/PDF de la cotizacion, o una lista de piezas:\n\nsonata 2018\nbonete\nfarol derecho\nbumper delantero'
      : '📸 Send a photo/PDF of the quote, or a parts list:\n\nsonata 2018\nhood\nheadlight right\nfront bumper');
    return;
  }

  // ── Unsupported message type ──
  if (message.audioMessage || message.videoMessage || message.stickerMessage) {
    await sendText(sock, jid, t(jid, 'only_photos'));
  }
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
startBot().catch(e => {
  consoleLog('Fatal error:', e);
  process.exit(1);
});
