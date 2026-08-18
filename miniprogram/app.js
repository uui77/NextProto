// app.js — AI录音卡 QS668 微信小程序
const { recorder } = require('./utils/recorder.js');

App({
  globalData: {
    // 连接的设备信息
    device: null,
    deviceId: null,
    // 录音笔状态
    battery: null,
    capacity: null,
    version: null,
    // ASR / LLM 配置（用户设置页填入，存 storage）
    asrProvider: '',      // 'aliyun' | ''
    asrApiKey: '',
    asrModel: 'paraformer-1',
    llmProvider: '',      // 'deepseek' | ''
    llmApiKey: '',
    llmModel: 'deepseek-v4-flash',
    // 实时转写状态
    realtimeText: '',
  },

  onLaunch() {
    // 从 storage 恢复 API 配置
    const cfg = wx.getStorageSync('apiConfig') || {};
    Object.assign(this.globalData, cfg);
    // 初始化录音笔管理器
    recorder.init();
    console.log('[App] AI录音卡小程序启动');
  },

  /** 保存 API 配置到 storage */
  saveApiConfig(cfg) {
    Object.assign(this.globalData, cfg);
    wx.setStorageSync('apiConfig', {
      asrProvider: this.globalData.asrProvider,
      asrApiKey: this.globalData.asrApiKey,
      asrModel: this.globalData.asrModel,
      llmProvider: this.globalData.llmProvider,
      llmApiKey: this.globalData.llmApiKey,
      llmModel: this.globalData.llmModel,
    });
  },
});
