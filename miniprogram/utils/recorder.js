// utils/recorder.js — 高层设备逻辑：请求/应答匹配、文件列表、下载、实时音频
// 对应 Python 版 recorder/device.py 的 Recorder 类
const P = require('./protocol.js');
const { BleTransport } = require('./ble.js');

const CMD_TIMEOUT = 5000;        // 普通请求应答超时
const LIST_IDLE_TIMEOUT = 1200;  // 列表最后一帧后空闲收尾
const DOWNLOAD_IDLE_TIMEOUT = 12000;

class RecorderError extends Error {
  constructor(msg) { super(msg); this.name = 'RecorderError'; }
}

class Recorder {
  constructor() {
    this.transport = new BleTransport();
    this._seq = new P.SeqGenerator();
    this._pendingReq = null;  // { type, cmd, resolve, reject, timer }
    this._fileList = [];
    this._listTimer = null;
    // 下载会话
    this._download = null;    // { name, data:[], timer, resolve, reject }
    // 实时音频
    this._rt = null;          // { filename, packets, received, audioChunks:[] }
    this.onLog = null;        // (level, msg) => void
    this.onRealtime = null;   // (event, payload) => void
    this.onDownloadProgress = null; // (received, total) => void
  }

  init() {
    // 帧分发
    this.transport.onFrame = (frame, source) => this._handleFrame(frame, source);
    this.transport.onDisconnect = () => {
      this._failAllPending('设备断开');
    };
    this.transport.monitorConnectionState();
  }

  get isConnected() { return this.transport.connected; }

  _log(level, msg) {
    console.log(`[Recorder:${level}] ${msg}`);
    if (this.onLog) this.onLog(level, msg);
  }

  // ============================================================ 发送命令
  async _sendCommand(type, cmd, params = null, expectCmd = null, timeout = CMD_TIMEOUT) {
    if (!this.isConnected) throw new RecorderError('设备未连接');
    const seq = this._seq.next();
    const frame = P.buildCommand(seq, type, cmd, params);
    // 设置期望应答
    const expectType = type;
    const expectC = expectCmd !== null ? expectCmd : cmd;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this._pendingReq && this._pendingReq.seq === seq) {
          this._pendingReq = null;
          reject(new RecorderError('等待设备应答超时'));
        }
      }, timeout);
      this._pendingReq = { seq, type: expectType, cmd: expectC, resolve, reject, timer };
      this.transport.write(frame).catch((err) => {
        clearTimeout(timer);
        this._pendingReq = null;
        reject(new RecorderError(`写入失败: ${err.errMsg || err}`));
      });
    });
  }

  /** 发送原始帧（调试用） */
  async sendRaw(type, cmd, params = null) {
    const seq = this._seq.next();
    const frame = P.buildCommand(seq, type, cmd, params);
    await this.transport.write(frame);
    return seq;
  }

  // ============================================================ 帧分发
  _handleFrame(frame, source) {
    // 优先匹配 pending 请求
    if (this._pendingReq) {
      const req = this._pendingReq;
      // 同 TYPE 且 CMD 匹配（或 ACK）
      if (frame.type === req.type && (frame.cmd === req.cmd || frame.isAck)) {
        clearTimeout(req.timer);
        this._pendingReq = null;
        req.resolve(frame);
        return;
      }
    }
    // 文件列表流
    if (frame.type === P.TYPE_FILE) {
      if (frame.cmd === P.FILE_LIST_DATA) {
        this._handleListData(frame);
        return;
      }
      if (frame.cmd === P.FILE_LIST_DONE) {
        this._finishList();
        return;
      }
      if (frame.cmd === P.FILE_IMPORT_START) {
        this._handleDownloadStart(frame);
        return;
      }
      if (frame.cmd === P.FILE_DATA) {
        this._handleDownloadData(frame);
        return;
      }
      if (frame.cmd === P.FILE_IMPORT_END) {
        this._handleDownloadEnd(frame);
        return;
      }
    }
    // 实时音频流
    if (frame.type === P.TYPE_REALTIME) {
      this._handleRealtimeFrame(frame);
      return;
    }
    // 按键事件（AE23）
    if (frame.type === P.TYPE_KEY && source === 'AE23') {
      if (this.onRealtime) this.onRealtime('key', { cmd: frame.cmd, body: frame.body });
      return;
    }
  }

  _failAllPending(reason) {
    if (this._pendingReq) {
      clearTimeout(this._pendingReq.timer);
      this._pendingReq.reject(new RecorderError(reason));
      this._pendingReq = null;
    }
    if (this._download) {
      clearTimeout(this._download.timer);
      this._download.reject(new RecorderError(reason));
      this._download = null;
    }
    if (this._listTimer) {
      clearTimeout(this._listTimer);
      this._listTimer = null;
    }
  }

  // ============================================================ 控制命令
  async getBattery() {
    const frame = await this._sendCommand(P.TYPE_CONTROL, P.CTRL_GET_BATTERY, null, P.CTRL_BATTERY_RESP);
    return P.decodeBattery(frame.body);
  }

  async getCapacity() {
    const frame = await this._sendCommand(P.TYPE_CONTROL, P.CTRL_GET_CAPACITY, null, P.CTRL_CAPACITY_RESP);
    return P.decodeCapacity(frame.body);
  }

  async getVersion() {
    const frame = await this._sendCommand(P.TYPE_CONTROL, P.CTRL_GET_VERSION, null, P.CTRL_VERSION_RESP);
    return new TextDecoder('ascii').decode(frame.body).replace(/\x00+$/, '');
  }

  async syncTime(date = new Date()) {
    const params = new ArrayBuffer(7);
    const view = new DataView(params);
    view.setUint16(0, date.getFullYear(), true);
    view.setUint8(2, date.getMonth() + 1);
    view.setUint8(3, date.getDate());
    view.setUint8(4, date.getHours());
    view.setUint8(5, date.getMinutes());
    view.setUint8(6, date.getSeconds());
    await this._sendCommand(P.TYPE_CONTROL, P.CTRL_SYNC_TIME, params);
  }

  // ============================================================ 文件列表
  async getFileList() {
    this._fileList = [];
    // 发送 2-0 请求
    const seq = this._seq.next();
    const frame = P.buildCommand(seq, P.TYPE_FILE, P.FILE_LIST_REQ);
    // 设置空闲收尾定时器
    this._listTimer = setTimeout(() => this._finishList(), LIST_IDLE_TIMEOUT);
    await this.transport.write(frame);
    // 等待列表完成（由 _finishList 的 promise 或超时触发）
    return new Promise((resolve) => {
      this._listResolve = resolve;
    });
  }

  _handleListData(frame) {
    const entries = P.decodeFileList(frame.body);
    this._fileList.push(...entries);
    this._log('INFO', `列表收到 ${entries.length} 条，累计 ${this._fileList.length} 条`);
    // 重置空闲定时器
    if (this._listTimer) clearTimeout(this._listTimer);
    this._listTimer = setTimeout(() => this._finishList(), LIST_IDLE_TIMEOUT);
  }

  _finishList() {
    if (this._listTimer) { clearTimeout(this._listTimer); this._listTimer = null; }
    if (this._listResolve) {
      const list = [...this._fileList];
      this._listResolve = null;
      this._log('INFO', `列表完成：共 ${list.length} 条`);
      Promise.resolve(list);
    }
  }

  // ============================================================ 文件下载
  /**
   * 下载文件。优先请求 .wav，失败换 .opus。
   * @param {P.FileEntry} entry 文件列表条目
   * @returns {Promise<{name, data:ArrayBuffer, isWav:boolean}>}
   */
  async download(entry) {
    const candidates = entry.candidateNames();
    for (const name of candidates) {
      try {
        this._log('INFO', `尝试下载: ${name}`);
        return await this._downloadByName(name);
      } catch (err) {
        if (err.message && err.message.includes('文件不存在')) {
          this._log('WARN', `${name} 不存在，换候选名`);
          continue;
        }
        throw err;
      }
    }
    throw new RecorderError('所有候选名均失败');
  }

  _downloadByName(name) {
    return new Promise((resolve, reject) => {
      const seq = this._seq.next();
      const frame = P.buildImportRequest(seq, name, 0);
      // 2-2 必须整帧单写
      this._download = {
        name, data: [], timer: null, resolve, reject,
      };
      // 空闲超时
      this._download.timer = setTimeout(() => {
        if (this._download) {
          this._log('ERR', `下载超时: ${name}`);
          this._download.reject(new RecorderError('下载超时'));
          this._download = null;
        }
      }, DOWNLOAD_IDLE_TIMEOUT);
      this.transport.write(frame).catch((err) => {
        if (this._download) {
          clearTimeout(this._download.timer);
          this._download.reject(new RecorderError(`写入失败: ${err.errMsg || err}`));
          this._download = null;
        }
      });
    });
  }

  _handleDownloadStart(frame) {
    if (!this._download) return;
    const name = new TextDecoder('utf-8').decode(frame.body).replace(/\x00+$/, '');
    this._log('INFO', `开始导入: ${name}`);
    this._download.name = name;
  }

  _handleDownloadData(frame) {
    if (!this._download) return;
    // 重置空闲定时器
    clearTimeout(this._download.timer);
    this._download.timer = setTimeout(() => {
      if (this._download) {
        this._download.reject(new RecorderError('下载超时'));
        this._download = null;
      }
    }, DOWNLOAD_IDLE_TIMEOUT);
    // 收集数据
    this._download.data.push(frame.body.slice(0)); // 复制
    const received = this._download.data.reduce((s, a) => s + a.byteLength, 0);
    if (this.onDownloadProgress) this.onDownloadProgress(received, 0);
  }

  _handleDownloadEnd(frame) {
    if (!this._download) return;
    clearTimeout(this._download.timer);
    const code = frame.body.length > 0 ? frame.body[0] : -1;
    const dl = this._download;
    this._download = null;
    if (code === P.IMPORT_END_OK) {
      // 合并所有 chunk
      const total = dl.data.reduce((s, a) => s + a.byteLength, 0);
      const merged = new Uint8Array(total);
      let off = 0;
      for (const chunk of dl.data) {
        merged.set(chunk, off);
        off += chunk.byteLength;
      }
      const isWav = P.isWav(merged);
      this._log('INFO', `下载完成: ${dl.name} ${total}B ${isWav ? 'WAV' : 'OPUS'}`);
      dl.resolve({ name: dl.name, data: merged.buffer, isWav });
    } else if (code === P.IMPORT_END_NOT_FOUND) {
      dl.reject(new RecorderError('文件不存在'));
    } else if (code === P.IMPORT_END_BAD_OFFSET) {
      dl.reject(new RecorderError('offset 过大'));
    } else {
      dl.reject(new RecorderError(`导入结束 code=${code}`));
    }
  }

  /** 终止下载 */
  async abortDownload() {
    const seq = this._seq.next();
    const frame = P.buildCommand(seq, P.TYPE_FILE, P.FILE_IMPORT_ABORT);
    await this.transport.write(frame);
    if (this._download) {
      clearTimeout(this._download.timer);
      this._download.reject(new RecorderError('已取消'));
      this._download = null;
    }
  }

  // ============================================================ 实时音频
  async realtimeStart() {
    const frame = await this._sendCommand(P.TYPE_REALTIME, P.RT_START, null, P.RT_START, CMD_TIMEOUT);
    // 设备应答中含本次录音文件名
    const name = new TextDecoder('utf-8').decode(frame.body).replace(/\x00+$/, '');
    this._rt = { filename: name, packets: 0, received: 0, audioChunks: [] };
    this._log('INFO', `实时转写开始，文件名: ${name}`);
    return name;
  }

  async realtimeStop() {
    const seq = this._seq.next();
    const frame = P.buildCommand(seq, P.TYPE_REALTIME, P.RT_STOP);
    await this.transport.write(frame);
    const rt = this._rt;
    this._rt = null;
    this._log('INFO', `实时转写停止，收到 ${rt ? rt.packets : 0} packet`);
    return rt;
  }

  async realtimePause() {
    const params = new ArrayBuffer(1);
    new DataView(params).setUint8(0, 1);
    await this._sendCommand(P.TYPE_REALTIME, P.RT_PAUSE_RESUME, params);
  }

  async realtimeResume() {
    const params = new ArrayBuffer(1);
    new DataView(params).setUint8(0, 0);
    await this._sendCommand(P.TYPE_REALTIME, P.RT_PAUSE_RESUME, params);
  }

  _handleRealtimeFrame(frame) {
    if (!this._rt) {
      // 延迟到达的帧，忽略
      return;
    }
    if (frame.cmd === P.RT_AUDIO_DATA) {
      this._rt.received += frame.body.byteLength;
      this._rt.packets++;
      this._rt.audioChunks.push(frame.body.slice(0));
      if (this.onRealtime) this.onRealtime('audio', frame.body);
    } else if (frame.cmd === P.RT_DEV_STATE) {
      const state = frame.body.length > 0 ? frame.body[0] : -1;
      if (state === 2) {
        // 设备端停止
        this._rt = null;
        this._log('INFO', '设备端停止实时推流');
      }
      if (this.onRealtime) this.onRealtime('state', state);
    } else if (frame.cmd === P.RT_START) {
      // 设备通告文件名（推流前）
      const name = new TextDecoder('utf-8').decode(frame.body).replace(/\x00+$/, '');
      if (this._rt) this._rt.filename = name;
      if (this.onRealtime) this.onRealtime('filename', name);
    }
  }

  /** 获取实时录音的完整 OPUS 码流（raw 40B packets） */
  getRealtimeRawOpus() {
    if (!this._rt || this._rt.audioChunks.length === 0) return null;
    const total = this._rt.audioChunks.reduce((s, a) => s + a.byteLength, 0);
    const merged = new Uint8Array(total);
    let off = 0;
    for (const chunk of this._rt.audioChunks) {
      merged.set(chunk, off);
      off += chunk.byteLength;
    }
    return { data: merged.buffer, packets: this._rt.packets, filename: this._rt.filename };
  }

  // ============================================================ 录音控制
  async recordStart() {
    const frame = await this._sendCommand(P.TYPE_KEY, P.KEY_REC_START, null, P.KEY_REC_START_RESP);
    return frame.body.length > 0 ? frame.body[0] : 0;
  }

  async recordSave() {
    const frame = await this._sendCommand(P.TYPE_KEY, P.KEY_REC_SAVE, null, P.KEY_REC_SAVE_RESP);
    return frame.body.length > 0 ? frame.body[0] : 0;
  }

  async recordPause() {
    const frame = await this._sendCommand(P.TYPE_KEY, P.KEY_REC_PAUSE, null, P.KEY_REC_PAUSE_RESP);
    return frame.body.length > 0 ? frame.body[0] : 0;
  }

  async recordResume() {
    const frame = await this._sendCommand(P.TYPE_KEY, P.KEY_REC_RESUME, null, P.KEY_REC_RESUME_RESP);
    return frame.body.length > 0 ? frame.body[0] : 0;
  }

  async getRecordState() {
    const frame = await this._sendCommand(P.TYPE_KEY, P.KEY_GET_STATE, null, P.KEY_STATE_RESP);
    return frame.body.length > 0 ? frame.body[0] : 0;
  }

  async getGain() {
    const frame = await this._sendCommand(P.TYPE_KEY, P.KEY_GET_GAIN, null, P.KEY_GAIN_RESP);
    return frame.body.length > 0 ? frame.body[0] : 0;
  }

  async setGain(value) {
    const params = new ArrayBuffer(1);
    new DataView(params).setUint8(0, value);
    const frame = await this._sendCommand(P.TYPE_KEY, P.KEY_SET_GAIN, params, P.KEY_SET_GAIN_RESP);
    return frame.body.length > 0 ? frame.body[0] : 0;
  }

  // ============================================================ 删除
  async deleteFile(entry) {
    const params = entry.raw.buffer.slice(entry.raw.byteOffset, entry.raw.byteOffset + P.LIST_ENTRY_LEN);
    const frame = await this._sendCommand(P.TYPE_FILE, P.FILE_DELETE_ONE, params, P.FILE_DELETE_ONE_RESP, 3000);
    return frame.body.length > 0 ? frame.body[0] : 0;
  }

  async deleteAllFiles() {
    const frame = await this._sendCommand(P.TYPE_FILE, P.FILE_DELETE_ALL, null, P.FILE_DELETE_ALL_RESP, 3000);
    return frame.body.length > 0 ? frame.body[0] : 0;
  }
}

// 单例
const recorder = new Recorder();
module.exports = { recorder, Recorder, RecorderError };
