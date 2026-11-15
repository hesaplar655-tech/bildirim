/**
 * Haber botu - Substack e-postalarini toplayan Gmail betigi (Google Apps Script)
 *
 * Kurulum: script.google.com > Yeni proje > bu kodu yapistir > ust menuden "setup" sec > Calistir.
 * Google izin isteyecek (Gmail'i okuma + disariya istek). Onaylayinca betik
 * 15 dakikada bir Substack'ten gelen yeni e-postalari bota yollar. Bilgisayarin kapali olmasi sorun degil.
 */

const WORKER = "https://haber-botu.haber-botu.workers.dev/ingest";
const KEY = "__INGEST_KEY__";

/** Bir kez calistir: 15 dakikalik zamanlayiciyi kurar ve ilk toplamayi yapar. */
function setup() {
  ScriptApp.getProjectTriggers().forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger("collect").timeBased().everyMinutes(15).create();
  collect();
}

/** Son calismadan bu yana gelen Substack yazilarini bota gonderir. */
function collect() {
  const props = PropertiesService.getScriptProperties();
  const last = Number(props.getProperty("last") || Date.now() - 2 * 24 * 3600 * 1000);
  const afterSec = Math.floor(last / 1000) - 3600; // 1 saat pay
  const threads = GmailApp.search(`from:substack.com after:${afterSec}`, 0, 100);

  const items = [];
  let newest = last;
  threads.forEach((thread) =>
    thread.getMessages().forEach((m) => {
      const t = m.getDate().getTime();
      if (t <= last) return;
      const item = parse(m);
      if (item) items.push(item);
      newest = Math.max(newest, t);
    })
  );

  if (items.length) {
    const res = UrlFetchApp.fetch(WORKER, {
      method: "post",
      contentType: "application/json",
      headers: { "X-Key": KEY },
      payload: JSON.stringify({ items }),
      muteHttpExceptions: true,
    });
    if (res.getResponseCode() !== 200) throw new Error("Bot cevabi: " + res.getContentText());
  }
  props.setProperty("last", String(newest));
  console.log(`${items.length} yazi gonderildi`);
}

/** Substack e-postasindan yazi bilgilerini cikarir. Yazi olmayan e-postalari (bildirim vb.) atlar. */
function parse(m) {
  const body = m.getPlainBody();
  const link = body.match(/View this post on the web at (\S+)/);
  if (!link) return null;

  let content = body.slice(link.index + link[0].length);
  const cut = content.search(/\n\s*Unsubscribe https?:\/\//);
  if (cut >= 0) content = content.slice(0, cut);
  content = content
    .replace(/\[https?:\/\/[^\]]+\]/g, "") // metin icindeki [link] kalintilari
    .replace(/^\s*(Subscribe now|Share|Leave a comment|Upgrade to paid)\s*$/gim, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  const from = m.getFrom();
  const name = (from.match(/^"?([^"<]+?)"?\s*</) || [null, from])[1].trim();
  return {
    source: name,
    title: m.getSubject(),
    url: link[1].split("?")[0],
    desc: "",
    content: content,
    published: m.getDate().toISOString(),
  };
}
