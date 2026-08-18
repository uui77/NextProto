// pages/transcribe/transcribe.js — 实时转写 + 文件转写
const app = getApp();
const { recorder, RecorderError } = require('../../utils/recorder.js');

Page({
  data: {
    // 模式：'realtime' | 'file'
    mode: 'realtime',
    filePath: '',
    fileName: '',

    // 实时录音状态
    recording: false,
    paused: false,
    rtFilename: '',
    packetCount: 0,
    elapsedSec: 0,
    elapsedTime: '00:00',

    // 转写结果
    transcribing: false,
    transcript: '',
    transcribeProgress: '',

    // AI 摘要
    summarizing: false,
    summary: '',

    // API 配置
    showConfig: false,
    asrProvider: '',
    asrApiKey: '',
    llmProvider: '',
    llmApiKey: '',
  },

  _timer: null,

  onLoad(query) {
    // 从文件页跳转：mode=file&path=xxx&name=xxx
    if (query.mode === 'file') {
      this.setData({
        mode: 'file',
        filePath: decodeURIComponent(query.path || ''),
        fileName: decodeURIComponent(query.name || ''),
      });
      // 自动开始转写
      setTimeout(() => this.transcribeFile(), 500);
    }
    // 恢复 API 配置
    const g = app.globalData;
    this.setData({
      asrProvider: g.asrProvider,
      asrApiKey: g.asrApiKey,
      llmProvider: g.llmProvider,
      llmApiKey: g.llmApiKey,
    });

    // 实时音频回调
    recorder.onRealtime = (event, payload) => {
      if (event === 'audio') {
        this.setData({ packetCount: this.data.packetCount + 1 });
      } else if (event === 'filename') {
        this.setData({ rtFilename: payload });
      } else if (event === 'state') {
        if (payload === 2) {
          // 设备端停止
          this.stopRecording(false);
        }
      }
    };
  },

  onUnload() {
    if (this._timer) clearInterval(this._timer);
    if (this.data.recording) {
      recorder.realtimeStop().catch(() => {});
    }
  },

  // ============================================================ 实时录音
  async startRecording() {
    if (this.data.recording) return;
    try {
      const name = await recorder.realtimeStart();
      this.setData({
        recording: true,
        paused: false,
        rtFilename: name,
        packetCount: 0,
        elapsedSec: 0,
        elapsedTime: '00:00',
        transcript: '',
        summary: '',
      });
      // 计时器
      this._timer = setInterval(() => {
        if (!this.data.paused) {
          const sec = this.data.elapsedSec + 1;
          const m = Math.floor(sec / 60);
          const s = sec % 60;
          const timeStr = `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
          this.setData({ elapsedSec: sec, elapsedTime: timeStr });
        }
      }, 1000);
    } catch (e) {
      wx.showToast({ title: `开始失败: ${e.message}`, icon: 'none', duration: 3000 });
    }
  },

  async stopRecording(sendStop = true) {
    if (!this.data.recording) return;
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    if (sendStop) {
      try { await recorder.realtimeStop(); } catch (e) { /* 忽略 */ }
    }
    this.setData({ recording: false, paused: false });
    // 自动保存 OPUS 文件并提示转写
    const raw = recorder.getRealtimeRawOpus();
    if (raw && raw.packets > 0) {
      const fs = wx.getFileSystemManager();
      const saveName = (raw.filename || `realtime-${Date.now()}`) + '.opus';
      const savePath = `${wx.env.USER_DATA_PATH}/${saveName}`;
      try {
        fs.writeFileSync(savePath, raw.data, 'binary');
        wx.showToast({ title: `已保存 ${raw.packets} 帧`, icon: 'none' });
        // 提示转写
        wx.showModal({
          title: '录音已保存',
          content: `文件：${saveName}\n收到 ${raw.packets} 个音频帧。\n是否转写成文字？`,
          confirmText: '转写',
          success: (res) => {
            if (res.confirm) {
              this.setData({ mode: 'file', filePath: savePath, fileName: saveName });
              this.transcribeFile();
            }
          },
        });
      } catch (e) {
        wx.showToast({ title: `保存失败: ${e.message}`, icon: 'none' });
      }
    }
  },

  async pauseRecording() {
    try {
      if (this.data.paused) {
        await recorder.realtimeResume();
        this.setData({ paused: false });
      } else {
        await recorder.realtimePause();
        this.setData({ paused: true });
      }
    } catch (e) {
      wx.showToast({ title: `操作失败: ${e.message}`, icon: 'none' });
    }
  },

  // ============================================================ 文件转写
  async transcribeFile() {
    if (this.data.transcribing) return;
    if (!this.data.filePath) {
      wx.showToast({ title: '没有文件', icon: 'none' });
      return;
    }
    // 检查 API 配置
    if (!this.data.asrApiKey) {
      this.setData({ showConfig: true });
      wx.showToast({ title: '请先配置 ASR API Key', icon: 'none' });
      return;
    }

    this.setData({ transcribing: true, transcribeProgress: '读取文件...', transcript: '' });

    try {
      // 1. 读取文件
      const fs = wx.getFileSystemManager();
      const fileData = fs.readFileSync(this.data.filePath);
      this.setData({ transcribeProgress: `文件 ${(fileData.byteLength/1024).toFixed(0)} KB，上传转写...` });

      // 2. 调用阿里云 ASR
      const text = await this.callAliyunASR(fileData);
      this.setData({
        transcript: text,
        transcribing: false,
        transcribeProgress: '',
      });
      wx.showToast({ title: '转写完成', icon: 'success' });
    } catch (e) {
      this.setData({ transcribing: false, transcribeProgress: '' });
      wx.showToast({ title: `转写失败: ${e.message}`, icon: 'none', duration: 3000 });
    }
  },

  /** 调用阿里云 Paraformer 录音文件识别 */
  async callAliyunASR(audioData) {
    const apiKey = this.data.asrApiKey;
    const model = app.globalData.asrModel || 'paraformer-1';

    // 方案1：使用阿里云百炼 API（兼容 OpenAI 格式）
    // POST https://dashscope.aliyuncs.com/compatible-mode/v1/audio/transcriptions
    return new Promise((resolve, reject) => {
      const base64 = wx.arrayBufferToBase64(audioData);
      wx.request({
        url: 'https://dashscope.aliyuncs.com/compatible-mode/v1/audio/transcriptions',
        method: 'POST',
        header: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
        },
        data: {
          model: model,
          file: base64,
          response_format: 'json',
        },
        success: (res) => {
          if (res.statusCode === 200 && res.data) {
            const text = res.data.text || res.data.result || '';
            resolve(text);
          } else {
            reject(new Error(`ASR ${res.statusCode}: ${JSON.stringify(res.data)}`));
          }
        },
        fail: (err) => reject(new Error(`网络错误: ${err.errMsg}`)),
      });
    });
  },

  // ============================================================ AI 摘要
  async generateSummary() {
    if (this.data.summarizing) return;
    if (!this.data.transcript) {
      wx.showToast({ title: '请先转写', icon: 'none' });
      return;
    }
    if (!this.data.llmApiKey) {
      this.setData({ showConfig: true });
      wx.showToast({ title: '请先配置 LLM API Key', icon: 'none' });
      return;
    }

    this.setData({ summarizing: true });
    try {
      const summary = await this.callDeepSeek(this.data.transcript);
      this.setData({ summary, summarizing: false });
      wx.showToast({ title: '摘要生成完成', icon: 'success' });
    } catch (e) {
      this.setData({ summarizing: false });
      wx.showToast({ title: `摘要失败: ${e.message}`, icon: 'none', duration: 3000 });
    }
  },

  /** 调用 DeepSeek 生成摘要 */
  callDeepSeek(text) {
    const apiKey = this.data.llmApiKey;
    const model = app.globalData.llmModel || 'deepseek-v4-flash';
    const prompt = `请根据以下会议录音转写内容，生成一份结构化摘要，包含：\n1. 核心要点（3-5条）\n2. 关键决策\n3. 待办事项\n\n转写内容：\n${text.slice(0, 8000)}`;

    return new Promise((resolve, reject) => {
      wx.request({
        url: 'https://api.deepseek.com/v1/chat/completions',
        method: 'POST',
        header: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
        },
        data: {
          model: model,
          messages: [
            { role: 'system', content: '你是专业的会议纪要助手，擅长从转写文本中提炼结构化摘要。' },
            { role: 'user', content: prompt },
          ],
          temperature: 0.3,
          max_tokens: 2000,
        },
        success: (res) => {
          if (res.statusCode === 200 && res.data.choices) {
            resolve(res.data.choices[0].message.content);
          } else {
            reject(new Error(`LLM ${res.statusCode}: ${JSON.stringify(res.data)}`));
          }
        },
        fail: (err) => reject(new Error(`网络错误: ${err.errMsg}`)),
      });
    });
  },

  // ============================================================ API 配置
  toggleConfig() {
    this.setData({ showConfig: !this.data.showConfig });
  },

  onAsrKeyInput(e) { this.setData({ asrApiKey: e.detail.value }); },
  onLlmKeyInput(e) { this.setData({ llmApiKey: e.detail.value }); },
  onAsrProviderChange(e) { this.setData({ asrProvider: e.detail.value }); },
  onLlmProviderChange(e) { this.setData({ llmProvider: e.detail.value }); },

  saveConfig() {
    app.saveApiConfig({
      asrProvider: this.data.asrProvider,
      asrApiKey: this.data.asrApiKey,
      llmProvider: this.data.llmProvider,
      llmApiKey: this.data.llmApiKey,
    });
    this.setData({ showConfig: false });
    wx.showToast({ title: '配置已保存', icon: 'success' });
  },

  // ============================================================ 工具
  copyTranscript() {
    if (!this.data.transcript) return;
    wx.setClipboardData({
      data: this.data.transcript,
      success: () => wx.showToast({ title: '已复制', icon: 'success' }),
    });
  },

  copySummary() {
    if (!this.data.summary) return;
    wx.setClipboardData({
      data: this.data.summary,
      success: () => wx.showToast({ title: '已复制', icon: 'success' }),
    });
  },

  formatTime(sec) {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  },
});
