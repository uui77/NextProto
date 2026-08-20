// utils/recorder.js — 高层设备逻辑：请求/应答匹配、文件列表、下载、实时音频
// 对应 Python 版 recorder/device.py 的 Recorder 类
const P = require('./protocol.js');
const { BleTransport } = require('./ble.js');

const CMD_TIMEOUT = 5000;
const LIST_IDLE_TIMEOUT = 1500;
const LIST_FIRST_DATA_TIMEOUT = 8000;
const DOWNLOAD_IDLE_TIMEOUT = 12000;

class RecorderError extends Error {
  constructor(msg) { super(msg); this.name = 'RecorderError'; }
}

class Recorder {
  constructor() {
    this.transport = new BleTransport();
    this._seq = new P.SeqGenerator();
    // (type, cmd) → { resolve, reject, timer }
    this._waiters = {};
    // seq → 同一等待者；当 type/cmd 匹配失败时按 seq 兜底
    this._seqWaiters = {};
    this._fileList = [];
    this._listTimer = null;
    this._listFirstDataTimer = null;
    this._listSafetyTimer = null;
    this._listFirstDataReceived = false;
    this._listResolve = null;
    this._listReject = null;
    this._download = null;
    this._rt = null;
    this.onLog = null;
    this.onRealtime = null;
    this.onDownloadProgress = null;
  }

  init() {
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

  _sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ============================================================ 发送命令
  async _sendCommand(type, cmd, params = null, expectCmd = null, timeout = CMD_TIMEOUT) {
    if (!this.isConnected) throw new RecorderError('设备未连接');
    const seq = this._seq.next();
    const frame = P.buildCommand(seq, type, cmd, params);
    const expectType = type;
    const expectC = expectCmd !== null ? expectCmd : cmd;
    const key = `${expectType}_${expectC}`;
    this._log('DEBUG', `发送命令 type=${expectType} cmd=${expectC} seq=${seq} (key=${key})`);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        // 超时清理：同时清除 type/cmd 等待者和 seq 等待者
        if (this._waiters[key]) {
          clearTimeout(this._waiters[key].timer);
          delete this._waiters[key];
        }
        if (this._seqWaiters[seq]) {
          clearTimeout(this._seqWaiters[seq].timer);
          delete this._seqWaiters[seq];
        }
        reject(new RecorderError(`等待应答超时 type=${expectType} cmd=${expectC} seq=${seq}`));
      }, timeout);
      const waiter = { seq, resolve, reject, timer };
      this._waiters[key] = waiter;
      this._seqWaiters[seq] = waiter;  // SEQ 兜底
      this.transport.write(frame).catch((err) => {
        clearTimeout(timer);
        delete this._waiters[key];
        delete this._seqWaiters[seq];
        reject(new RecorderError(`写入失败: ${err.errMsg || err}`));
      });
    });
  }

  async sendRaw(type, cmd, params = null) {
    const seq = this._seq.next();
    const frame = P.buildCommand(seq, type, cmd, params);
    await this.transport.write(frame);
    return seq;
  }

  // ============================================================ 帧分发
  // 与 Python 版保持一致：特殊帧（列表/下载/实时）优先于 waiter 匹配
  _handleFrame(frame, source) {
    const type = frame.type;
    const cmd = frame.cmd;
    const key = `${type}_${cmd}`;
    const bodyHex = frame.body && frame.body.length > 0
      ? Array.from(frame.body.slice(0, Math.min(32, frame.body.length)))
          .map(b => b.toString(16).padStart(2, '0')).join(' ')
      : '(empty)';
    this._log('DEBUG', `收到帧 source=${source} seq=${frame.seq} type=${type} cmd=${cmd} bodyLen=${frame.body.length} body=${bodyHex}`);

    // 0) 调试：所有 TYPE_FILE 帧都记录
    if (type === P.TYPE_FILE) {
      this._log('DEBUG', `文件帧 cmd=${cmd} isAck=${frame.isAck} bodyLen=${frame.body.length}`);
    }

    // 1) 文件列表帧：始终优先处理，不允许被 waiter 消耗
    if (type === P.TYPE_FILE && cmd === P.FILE_LIST_DATA) {
      this._log('INFO', '→ 路由到 _handleListData');
      this._handleListData(frame);
      return;
    }
    if (type === P.TYPE_FILE && cmd === P.FILE_LIST_DONE) {
      this._log('INFO', '→ 路由到 _finishList (FILE_LIST_DONE)');
      this._finishList();
      return;
    }

    // 2) 文件下载流：需要下载会话存在
    if (type === P.TYPE_FILE && cmd === P.FILE_IMPORT_START) {
      if (this._download) this._handleDownloadStart(frame);
      else this._log('WARN', `收到 IMPORT_START 但无下载会话`);
      return;
    }
    if (type === P.TYPE_FILE && cmd === P.FILE_DATA) {
      if (this._download) this._handleDownloadData(frame);
      else this._log('WARN', `收到 FILE_DATA 但无下载会话，${frame.body.length}B 已忽略`);
      return;
    }
    if (type === P.TYPE_FILE && cmd === P.FILE_IMPORT_END) {
      if (this._download) this._handleDownloadEnd(frame);
      return;
    }

    // 3) 实时音频流
    if (type === P.TYPE_REALTIME) {
      this._handleRealtimeFrame(frame);
      return;
    }

    // 4) 按键事件（AE23 通道）
    if (type === P.TYPE_KEY && source === 'AE23') {
      if (this.onRealtime) this.onRealtime('key', { cmd, body: frame.body });
      return;
    }

    // 5) 一次性请求应答：匹配 waiter
    let waiter = this._waiters[key];
    if (!waiter && frame.seq !== undefined && this._seqWaiters[frame.seq]) {
      waiter = this._seqWaiters[frame.seq];
      if (waiter) {
        this._log('DEBUG', `SEQ 兜底匹配 seq=${frame.seq} type=${type} cmd=${cmd}`);
      }
    }
    if (waiter) {
      clearTimeout(waiter.timer);
      delete this._waiters[key];
      for (const k of Object.keys(this._waiters)) {
        if (this._waiters[k] === waiter) delete this._waiters[k];
      }
      if (frame.seq !== undefined) delete this._seqWaiters[frame.seq];
      if (waiter.seq !== undefined) delete this._seqWaiters[waiter.seq];
      this._log('DEBUG', `应答匹配 type=${type} cmd=${cmd} seq=${frame.seq}`);
      waiter.resolve(frame);
      return;
    }

    // 6) ACK 帧：长度=1 字节（仅 TYPE），无等待者时忽略
    if (frame.isAck) {
      this._log('DEBUG', `ACK type=${type} seq=${frame.seq}（无等待者，忽略）`);
      return;
    }

    // 7) 未匹配帧（复用顶部已声明的 bodyHex）
    this._log('WARN', `未匹配帧 source=${source} seq=${frame.seq} type=${type} cmd=${cmd} body=${bodyHex}${frame.body && frame.body.length > 32 ? '...' : ''}`);
  }

  _failAllPending(reason) {
    // 先收集所有唯一 waiter（两个字典可能指向同一对象）
    const uniqueWaiters = new Set();
    for (const w of Object.values(this._waiters)) uniqueWaiters.add(w);
    for (const w of Object.values(this._seqWaiters)) uniqueWaiters.add(w);
    for (const w of uniqueWaiters) {
      clearTimeout(w.timer);
      w.reject(new RecorderError(reason));
    }
    this._waiters = {};
    this._seqWaiters = {};
    if (this._download) {
      clearTimeout(this._download.timer);
      this._download.reject(new RecorderError(reason));
      this._download = null;
    }
    // 清理文件列表请求
    this._cleanupListTimers();
    if (this._listReject) {
      this._listReject(new RecorderError(reason));
      this._listResolve = null;
      this._listReject = null;
    }
    // 清理实时转写状态
    if (this._rt) {
      if (this._rt.onError) this._rt.onError(reason);
      this._rt = null;
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
    // 0-0 命令：与 Python 版一致，fire-and-forget，设备可能不回包
    await this.sendRaw(P.TYPE_CONTROL, P.CTRL_SYNC_TIME, params);
    this._log('INFO', '时间同步命令已发送');
  }

  // ============================================================ 文件列表
  async getFileList() {
    if (this._listResolve) {
      this._log('WARN', '已有列表请求进行中，忽略本次请求');
      throw new RecorderError('已有列表请求进行中，请稍后重试');
    }
    this._fileList = [];
    this._listFirstDataReceived = false;
    const seq = this._seq.next();
    const frame = P.buildCommand(seq, P.TYPE_FILE, P.FILE_LIST_REQ);
    this._log('INFO', `发送文件列表请求 (2-0) seq=${seq}`);
    return new Promise((resolve, reject) => {
      this._listResolve = resolve;
      this._listReject = reject;

      // 安全兜底：15 秒总超时
      this._listSafetyTimer = setTimeout(() => {
        this._log('WARN', '列表请求总超时 (15s)，强制结束');
        this._finishList();
      }, 15000);

      // 阶段1：等待 BLE 写入完成
      this.transport.write(frame).then(() => {
        this._log('INFO', '文件列表请求写入成功，等待设备响应...');
        // 阶段2：等待首个文件数据帧（仅当尚未收到首帧时）
        if (!this._listFirstDataReceived) {
          this._listFirstDataTimer = setTimeout(() => {
            if (!this._listFirstDataReceived) {
              this._log('WARN', `首帧数据超时 (${LIST_FIRST_DATA_TIMEOUT}ms)，设备可能无文件或未响应`);
              this._finishList();
            }
          }, LIST_FIRST_DATA_TIMEOUT);
        } else {
          this._log('INFO', '写入完成时首帧数据已到达，直接进入收集阶段');
          // 首帧已到达，确保空闲计时器在运行
          if (!this._listTimer) {
            this._listTimer = setTimeout(() => {
              this._log('INFO', `列表空闲超时 (${LIST_IDLE_TIMEOUT}ms)，结束收集`);
              this._finishList();
            }, LIST_IDLE_TIMEOUT);
          }
        }
      }).catch((err) => {
        this._cleanupListTimers();
        this._listResolve = null;
        this._listReject = null;
        reject(new RecorderError(`列表请求写入失败: ${err.errMsg || err}`));
      });
    });
  }

  _cleanupListTimers() {
    if (this._listTimer) { clearTimeout(this._listTimer); this._listTimer = null; }
    if (this._listFirstDataTimer) { clearTimeout(this._listFirstDataTimer); this._listFirstDataTimer = null; }
    if (this._listSafetyTimer) { clearTimeout(this._listSafetyTimer); this._listSafetyTimer = null; }
  }

  _handleListData(frame) {
    if (!this._listResolve) {
      this._log('WARN', `收到列表数据但无等待者（可能已完成），忽略 ${frame.body.length}B`);
      return;
    }
    const bodyLen = frame.body.length;
    this._log('DEBUG', `_handleListData 开始解析 bodyLen=${bodyLen}`);
    if (bodyLen < 4) {
      this._log('WARN', `列表帧 body 太短 (${bodyLen}B)，至少需要 4B 存储 count`);
      return;
    }
    const view = new DataView(frame.body.buffer, frame.body.byteOffset);
    const count = view.getUint32(0, false); // BE
    this._log('DEBUG', `声明条目数 count=${count}`);
    if (count > 10000) {
      this._log('WARN', `count 异常大 (${count})，可能是字节序错误，尝试 LE 解释`);
      const leCount = view.getUint32(0, true);
      this._log('WARN', `LE 解释 count=${leCount}`);
    }
    const entries = P.decodeFileList(frame.body);
    this._fileList.push(...entries);
    this._log('INFO', `列表收到 ${entries.length} 条，累计 ${this._fileList.length} 条 (bodyLen=${bodyLen})`);
    // 首帧数据到达：取消首帧等待计时器，启动空闲计时器
    if (!this._listFirstDataReceived) {
      this._listFirstDataReceived = true;
      if (this._listFirstDataTimer) {
        clearTimeout(this._listFirstDataTimer);
        this._listFirstDataTimer = null;
      }
      this._log('INFO', '首个文件列表数据帧已到达，开始收集...');
    }
    // 重置空闲计时器（每帧都刷新）
    if (this._listTimer) clearTimeout(this._listTimer);
    this._listTimer = setTimeout(() => {
      this._log('INFO', `列表空闲超时 (${LIST_IDLE_TIMEOUT}ms)，结束收集`);
      this._finishList();
    }, LIST_IDLE_TIMEOUT);
  }

  _finishList() {
    this._cleanupListTimers();
    if (this._listResolve) {
      const list = [...this._fileList];
      const resolveFn = this._listResolve;
      this._listResolve = null;
      this._listReject = null;
      this._log('INFO', `列表完成：共 ${list.length} 条 (首次数据到达=${this._listFirstDataReceived})`);
      resolveFn(list);
    } else {
      this._log('DEBUG', '_finishList 被调用但无等待者（可能已超时或重复调用）');
    }
  }

  // ============================================================ 文件下载
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
      this._download = {
        name, data: [], timer: null, resolve, reject,
      };
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
    clearTimeout(this._download.timer);
    this._download.timer = setTimeout(() => {
      if (this._download) {
        this._download.reject(new RecorderError('下载超时'));
        this._download = null;
      }
    }, DOWNLOAD_IDLE_TIMEOUT);
    this._download.data.push(frame.body.slice(0));
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
    if (this._rt) throw new RecorderError('实时会话已在进行中');
    // 实时开始命令：直接发送，不等应答（设备随后会通过 Notify 推流，并通告文件名）
    await this.sendRaw(P.TYPE_REALTIME, P.RT_START);
    this._rt = { filename: '', packets: 0, received: 0, audioChunks: [], startedAt: Date.now() };
    this._log('INFO', '实时转写命令已发送，等待设备推流...');
    // 等最多 3s，看是否收到 RT_START 应答（或直接开始收到 RT_AUDIO_DATA）
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (!this._rt || !this._rt.filename) {
          this._log('WARN', '实时转写未在预期时间内收到文件名通告，但将继续等待音频数据');
        }
        resolve();
      }, 3000);
      const check = setInterval(() => {
        if (this._rt && this._rt.filename) {
          clearTimeout(timer);
          clearInterval(check);
          resolve();
        }
      }, 100);
    });
    const name = this._rt ? (this._rt.filename || '实时录音') : '实时录音';
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
    if (!this._rt) return;
    if (frame.cmd === P.RT_AUDIO_DATA) {
      this._rt.received += frame.body.byteLength;
      this._rt.packets++;
      this._rt.audioChunks.push(frame.body.slice(0));
      if (this.onRealtime) this.onRealtime('audio', frame.body);
    } else if (frame.cmd === P.RT_DEV_STATE) {
      const state = frame.body.length > 0 ? frame.body[0] : -1;
      if (state === 2) {
        this._rt = null;
        this._log('INFO', '设备端停止实时推流');
      }
      if (this.onRealtime) this.onRealtime('state', state);
    } else if (frame.cmd === P.RT_START) {
      const name = new TextDecoder('utf-8').decode(frame.body).replace(/\x00+$/, '');
      if (this._rt) this._rt.filename = name;
      if (this.onRealtime) this.onRealtime('filename', name);
    }
  }

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

const recorder = new Recorder();
module.exports = { recorder, Recorder, RecorderError };
