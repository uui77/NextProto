// pages/scan/scan.js — 扫描并连接录音笔
const app = getApp();
const { recorder } = require('../../utils/recorder.js');

Page({
  data: {
    scanning: false,
    devices: [],
    connected: false,
    connecting: false,
    logs: [],
    compatScan: false,
    deviceInfo: null,
    battery: null,
    capacity: null,
    capacityText: '',
    version: null,
  },

  onLoad() {
    recorder.onLog = (level, msg) => {
      this.addLog(level, msg);
    };
    // 初始化蓝牙适配器
    this.initBluetooth();
  },

  onUnload() {
    if (this.data.scanning) {
      recorder.transport.stopScan();
    }
  },

  async initBluetooth() {
    try {
      await this.openBluetoothAdapter();
      this.addLog('INFO', '蓝牙适配器已就绪');
    } catch (e) {
      this.addLog('ERR', `蓝牙初始化失败: ${e.errMsg || e}`);
    }
  },

  openBluetoothAdapter() {
    return new Promise((resolve, reject) => {
      wx.openBluetoothAdapter({
        success: () => resolve(),
        fail: (err) => {
          if (err.errCode === 10001) {
            this.addLog('WARN', '蓝牙未开启，请打开手机蓝牙');
          }
          reject(err);
        },
      });
    });
  },

  async onScan() {
    if (this.data.scanning) return;
    this.setData({ scanning: true, devices: [] });
    this.addLog('INFO', `扫描中（6 秒）${this.data.compatScan ? ' [兼容模式]' : ''}...`);
    try {
      const devices = this.data.compatScan
        ? await recorder.transport.scanAll(6000)
        : await recorder.transport.scan(6000);
      // 过滤无名字设备（兼容模式下）
      const filtered = devices.filter(d => d.name || d.localName);
      this.setData({ devices: filtered, scanning: false });
      if (filtered.length === 0) {
        this.addLog('WARN', '未发现录音笔；可勾选兼容扫描重试');
      } else {
        this.addLog('INFO', `发现 ${filtered.length} 个设备`);
      }
    } catch (e) {
      this.setData({ scanning: false });
      this.addLog('ERR', `扫描失败: ${e.errMsg || e}`);
    }
  },

  onCompatChange(e) {
    this.setData({ compatScan: e.detail.value });
  },

  async onConnectDevice(e) {
    const deviceId = e.currentTarget.dataset.id;
    const device = this.data.devices.find(d => d.deviceId === deviceId);
    if (!device) return;
    this.setData({ connecting: true });
    this.addLog('INFO', `连接中...`);
    try {
      await recorder.transport.connect(deviceId);
      this.addLog('INFO', '已连接，同步时间...');
      // 同步时间
      try { await recorder.syncTime(); } catch (e) { /* 非致命 */ }
      // 获取设备信息
      await this.fetchDeviceInfo();
      app.globalData.deviceId = deviceId;
      app.globalData.device = device;
      this.setData({ connected: true, connecting: false });
      this.addLog('INFO', '设备就绪');
    } catch (e) {
      this.setData({ connecting: false });
      this.addLog('ERR', `连接失败: ${e.errMsg || e}`);
    }
  },

  async fetchDeviceInfo() {
    try {
      const battery = await recorder.getBattery();
      this.setData({ battery });
      this.addLog('INFO', `电量: ${battery}%`);
    } catch (e) { this.addLog('WARN', `电量获取失败`); }
    try {
      const cap = await recorder.getCapacity();
      const capText = `${(cap.remain/1048576).toFixed(0)} / ${(cap.total/1048576).toFixed(0)} MB`;
      this.setData({ capacity: cap, capacityText: capText });
      this.addLog('INFO', `容量: ${(cap.remain/1048576).toFixed(1)}MB / ${(cap.total/1048576).toFixed(1)}MB`);
    } catch (e) { this.addLog('WARN', `容量获取失败`); }
    try {
      const ver = await recorder.getVersion();
      this.setData({ version: ver });
      this.addLog('INFO', `固件: ${ver}`);
    } catch (e) { this.addLog('WARN', `版本获取失败`); }
  },

  onDisconnect() {
    wx.showModal({
      title: '断开连接',
      content: '确定断开录音笔连接？',
      success: async (res) => {
        if (res.confirm) {
          await recorder.transport.disconnect();
          this.setData({ connected: false, deviceInfo: null, battery: null, capacity: null });
          app.globalData.deviceId = null;
          this.addLog('INFO', '已断开连接');
        }
      },
    });
  },

  goFiles() {
    wx.navigateTo({ url: '/pages/files/files' });
  },

  goTranscribe() {
    wx.navigateTo({ url: '/pages/transcribe/transcribe' });
  },

  addLog(level, msg) {
    const time = new Date().toTimeString().slice(0, 8);
    const line = `[${time}] [${level}] ${msg}`;
    const logs = [...this.data.logs, { level, text: line }];
    if (logs.length > 200) logs.shift();
    this.setData({ logs });
  },
});
