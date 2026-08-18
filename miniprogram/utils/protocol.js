// utils/protocol.js — 命令常量、帧构造、流式解析、字段解码
// 对应 Python 版 recorder/protocol.py
const { crc16Xmodem } = require('./crc16.js');

// ================================================================ 常量
const MAGIC = 0x5A;
const HEADER_LEN = 6;
const MAX_DATA_LEN = 8192;

// DATA 类型
const TYPE_CONTROL = 0;
const TYPE_REALTIME = 1;
const TYPE_FILE = 2;
const TYPE_KEY = 3;

// 控制命令 TYPE=0
const CTRL_SYNC_TIME = 0;
const CTRL_GET_CAPACITY = 1;
const CTRL_CAPACITY_RESP = 2;
const CTRL_GET_BATTERY = 3;
const CTRL_BATTERY_RESP = 4;
const CTRL_GET_VERSION = 10;
const CTRL_VERSION_RESP = 11;
const CTRL_GET_AUTH = 12;
const CTRL_AUTH_RESP = 13;
const BATTERY_CHARGING = 110;

// 实时音频命令 TYPE=1
const RT_START = 0;
const RT_AUDIO_DATA = 1;
const RT_STOP = 2;
const RT_PAUSE_RESUME = 3;
const RT_DEV_STATE = 4;

// 文件命令 TYPE=2
const FILE_LIST_REQ = 0;
const FILE_LIST_DATA = 1;
const FILE_IMPORT_REQ = 2;
const FILE_IMPORT_START = 3;
const FILE_DATA = 4;
const FILE_IMPORT_END = 5;
const FILE_IMPORT_ABORT = 7;
const FILE_DELETE_ONE = 8;
const FILE_DELETE_ALL = 9;
const FILE_DELETE_ALL_RESP = 10;
const FILE_ABORT_RESP = 11;
const FILE_IMPORT_SEG = 12;
const FILE_DELETE_ONE_RESP = 13;
const FILE_LIST_DONE = 18;

// 导入结束状态码
const IMPORT_END_OK = 0;
const IMPORT_END_NOT_FOUND = 1;
const IMPORT_END_BAD_OFFSET = 2;
const IMPORT_END_STOPPED = 3;

// 按键/录音控制 TYPE=3
const KEY_REC_START = 1;
const KEY_REC_START_RESP = 2;
const KEY_REC_SAVE = 3;
const KEY_REC_SAVE_RESP = 4;
const KEY_REC_PAUSE = 5;
const KEY_REC_PAUSE_RESP = 6;
const KEY_REC_RESUME = 7;
const KEY_REC_RESUME_RESP = 8;
const KEY_GET_STATE = 19;
const KEY_STATE_RESP = 20;
const KEY_GET_TIME = 21;
const KEY_TIME_RESP = 22;
const KEY_GET_FILENAME = 23;
const KEY_FILENAME_RESP = 24;
const KEY_GET_GAIN = 25;
const KEY_GAIN_RESP = 26;
const KEY_SET_GAIN = 27;
const KEY_SET_GAIN_RESP = 28;

const FILENAME_FIELD_LEN = 24;
const LIST_NAME_LEN = 20;
const LIST_ENTRY_LEN = 28;

// ================================================================ 帧构造
class SeqGenerator {
  constructor() { this._seq = -1; }
  next() { this._seq = (this._seq + 1) & 0xFF; return this._seq; }
}

/**
 * 构造完整协议帧：MAGIC + SEQ + CRC(LE) + LEN(LE) + DATA
 * @returns {ArrayBuffer}
 */
function buildFrame(seq, data) {
  const dataLen = data.byteLength;
  const buf = new ArrayBuffer(HEADER_LEN + dataLen);
  const view = new DataView(buf);
  const bytes = new Uint8Array(buf);
  view.setUint8(0, MAGIC);
  view.setUint8(1, seq & 0xFF);
  // LEN (LE)
  view.setUint16(4, dataLen, true);
  // DATA
  bytes.set(new Uint8Array(data), HEADER_LEN);
  // CRC(LE) = crc16(LEN原始2B + DATA)
  const crcInput = new Uint8Array(2 + dataLen);
  crcInput[0] = dataLen & 0xFF;
  crcInput[1] = (dataLen >> 8) & 0xFF;
  crcInput.set(new Uint8Array(data), 2);
  view.setUint16(2, crc16Xmodem(crcInput), true);
  return buf;
}

/** 构造 DATA=[TYPE][CMD][PARAMS...] 的命令帧 */
function buildCommand(seq, type, cmd, params = null) {
  const paramLen = params ? params.byteLength : 0;
  const data = new ArrayBuffer(2 + paramLen);
  const bytes = new Uint8Array(data);
  bytes[0] = type;
  bytes[1] = cmd;
  if (params) bytes.set(new Uint8Array(params), 2);
  return buildFrame(seq, data);
}

/** 编码文件名为固定 24B 字段，NUL 填充 */
function encodeFilename24(name) {
  const enc = new TextEncoder();
  let raw = enc.encode(name).slice(0, FILENAME_FIELD_LEN);
  const buf = new ArrayBuffer(FILENAME_FIELD_LEN);
  const bytes = new Uint8Array(buf);
  bytes.set(raw);
  return buf;
}

/** 构造 2-2 文件导入请求帧（完整 36B，必须一次 GATT 写入） */
function buildImportRequest(seq, filename, offset = 0) {
  const params = new ArrayBuffer(4 + FILENAME_FIELD_LEN);
  const view = new DataView(params);
  view.setUint32(0, offset, true); // LE
  const nameBuf = encodeFilename24(filename);
  new Uint8Array(params).set(new Uint8Array(nameBuf), 4);
  return buildCommand(seq, TYPE_FILE, FILE_IMPORT_REQ, params);
}

// ================================================================ 帧解析
class Frame {
  constructor(seq, data) {
    this.seq = seq;
    this.data = data; // Uint8Array
  }
  get type() { return this.data[0]; }
  get cmd() { return this.data.length >= 2 ? this.data[1] : null; }
  get isAck() { return this.data.length === 1; }
  get body() { return this.data.length >= 2 ? this.data.subarray(2) : new Uint8Array(0); }
}

/**
 * 流式帧解析器。AE22 与 AE23 必须各用一个独立实例。
 */
class FrameParser {
  constructor(name = '') {
    this.name = name;
    this._buf = [];       // number[] 逐字节缓冲
    this.crcErrors = 0;
  }

  /** 喂入一段通知字节，产出所有可完整解析的帧 */
  feed(chunk) {
    const bytes = new Uint8Array(chunk);
    for (let i = 0; i < bytes.length; i++) this._buf.push(bytes[i]);
    const frames = [];
    while (true) {
      const frame = this._tryParseOne();
      if (frame === null) break;
      frames.push(frame);
    }
    return frames;
  }

  _tryParseOne() {
    const buf = this._buf;
    // 丢弃 MAGIC 之前的噪声
    while (buf.length > 0 && buf[0] !== MAGIC) buf.shift();
    if (buf.length < HEADER_LEN) return null;

    const seq = buf[1];
    const crcRecv = (buf[2] | (buf[3] << 8)) & 0xFFFF;   // LE
    const length = (buf[4] | (buf[5] << 8)) & 0xFFFF;     // LE
    if (length > MAX_DATA_LEN) { buf.shift(); return this._tryParseOne(); }
    if (buf.length < HEADER_LEN + length) return null;

    // 提取 DATA
    const data = new Uint8Array(length);
    for (let i = 0; i < length; i++) data[i] = buf[HEADER_LEN + i];

    // CRC 校验
    const crcInput = new Uint8Array(2 + length);
    crcInput[0] = buf[4];
    crcInput[1] = buf[5];
    crcInput.set(data, 2);
    const crcCalc = crc16Xmodem(crcInput);
    if (crcCalc !== crcRecv) {
      this.crcErrors++;
      buf.splice(0, HEADER_LEN + length);
      console.warn(`[Parser:${this.name}] CRC mismatch seq=${seq}`);
      return this._tryParseOne();
    }

    buf.splice(0, HEADER_LEN + length);
    if (length === 0) return this._tryParseOne();
    return new Frame(seq, data);
  }

  reset() { this._buf = []; }
}

// ================================================================ 字段解码
/** 文件列表条目 */
class FileEntry {
  constructor(duration, size, name, raw) {
    this.duration = duration;
    this.size = size;
    this.name = name;
    this.raw = raw;
  }
  /** 下载候选文件名：优先 base.wav，其次 base.opus */
  candidateNames() {
    let base = this.name.replace(/\.+$/, '');
    for (const ext of ['.wav', '.opus', '.mp3']) {
      if (base.toLowerCase().endsWith(ext)) {
        base = base.slice(0, -ext.length);
        break;
      }
    }
    return [base + '.wav', base + '.opus', this.name];
  }
  /** WAV 进度估算：时长 × 32000 B/s + 44 */
  get estimatedWavSize() { return this.duration * 32000 + 44; }
}

/** 解码 2-1 文件列表帧 body */
function decodeFileList(body) {
  if (body.length < 4) return [];
  const view = new DataView(body.buffer, body.byteOffset);
  const count = view.getUint32(0, false); // BE
  const entries = [];
  let offset = 4;
  for (let i = 0; i < count; i++) {
    if (offset + LIST_ENTRY_LEN > body.length) break;
    const duration = view.getUint32(offset, false);     // BE
    const size = view.getUint32(offset + 4, false);      // BE
    const nameBytes = body.subarray(offset + 8, offset + 8 + LIST_NAME_LEN);
    const nulIdx = nameBytes.indexOf(0);
    const nameStr = new TextDecoder('utf-8').decode(
      nulIdx >= 0 ? nameBytes.subarray(0, nulIdx) : nameBytes);
    entries.push(new FileEntry(duration, size, nameStr,
      body.subarray(offset, offset + LIST_ENTRY_LEN)));
    offset += LIST_ENTRY_LEN;
  }
  return entries;
}

/** 解码 0-2 容量应答：remain:4B LE + total:4B LE */
function decodeCapacity(body) {
  const view = new DataView(body.buffer, body.byteOffset);
  return { remain: view.getUint32(0, true), total: view.getUint32(4, true) };
}

/** 解码 0-4 电量应答 */
function decodeBattery(body) { return body[0]; }

/** 解码 3-22 录音时间应答 */
function decodeRecordTime(body) {
  const view = new DataView(body.buffer, body.byteOffset);
  return { duration: view.getUint16(0, true), size: view.getUint32(2, true) };
}

/** 校验 WAV 头 */
function isWav(data) {
  return data.length >= 12 &&
    data[0] === 0x52 && data[1] === 0x49 && data[2] === 0x46 && data[3] === 0x46 && // RIFF
    data[8] === 0x57 && data[9] === 0x41 && data[10] === 0x56 && data[11] === 0x45;  // WAVE
}

module.exports = {
  MAGIC, HEADER_LEN, MAX_DATA_LEN,
  // Types
  TYPE_CONTROL, TYPE_REALTIME, TYPE_FILE, TYPE_KEY,
  // Control
  CTRL_SYNC_TIME, CTRL_GET_CAPACITY, CTRL_CAPACITY_RESP,
  CTRL_GET_BATTERY, CTRL_BATTERY_RESP, CTRL_GET_VERSION, CTRL_VERSION_RESP,
  CTRL_GET_AUTH, CTRL_AUTH_RESP, BATTERY_CHARGING,
  // Realtime
  RT_START, RT_AUDIO_DATA, RT_STOP, RT_PAUSE_RESUME, RT_DEV_STATE,
  // File
  FILE_LIST_REQ, FILE_LIST_DATA, FILE_IMPORT_REQ, FILE_IMPORT_START,
  FILE_DATA, FILE_IMPORT_END, FILE_IMPORT_ABORT, FILE_DELETE_ONE,
  FILE_DELETE_ALL, FILE_DELETE_ALL_RESP, FILE_ABORT_RESP, FILE_IMPORT_SEG,
  FILE_DELETE_ONE_RESP, FILE_LIST_DONE,
  IMPORT_END_OK, IMPORT_END_NOT_FOUND, IMPORT_END_BAD_OFFSET, IMPORT_END_STOPPED,
  // Key
  KEY_REC_START, KEY_REC_START_RESP, KEY_REC_SAVE, KEY_REC_SAVE_RESP,
  KEY_REC_PAUSE, KEY_REC_PAUSE_RESP, KEY_REC_RESUME, KEY_REC_RESUME_RESP,
  KEY_GET_STATE, KEY_STATE_RESP, KEY_GET_TIME, KEY_TIME_RESP,
  KEY_GET_FILENAME, KEY_FILENAME_RESP, KEY_GET_GAIN, KEY_GAIN_RESP,
  KEY_SET_GAIN, KEY_SET_GAIN_RESP,
  // Consts
  FILENAME_FIELD_LEN, LIST_NAME_LEN, LIST_ENTRY_LEN,
  // Frame
  SeqGenerator, buildFrame, buildCommand, encodeFilename24, buildImportRequest,
  Frame, FrameParser,
  // Decode
  FileEntry, decodeFileList, decodeCapacity, decodeBattery, decodeRecordTime, isWav,
};
