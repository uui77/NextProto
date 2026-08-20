// pages/files/files.js — 文件列表与下载
const app = getApp();
const { recorder, RecorderError } = require('../../utils/recorder.js');

Page({
  data: {
    files: [],
    loading: false,
    downloading: false,
    downloadProgress: '',
    downloadedFiles: [],  // [{name, path, size, time}]
    debugLogs: [],
    showDebug: false,
  },

  onLoad() {
    if (!recorder.isConnected) {
      wx.showToast({ title: '设备未连接', icon: 'none' });
      setTimeout(() => wx.navigateBack(), 1000);
      return;
    }
    // 设置日志回调，在页面显示调试日志
    recorder.onLog = (level, msg) => {
      this.addDebugLog(level, msg);
    };
    this.loadFiles();
  },

  onUnload() {
    recorder.onLog = null;
  },

  addDebugLog(level, msg) {
    const time = new Date().toTimeString().slice(0, 8);
    const line = `[${time}] [${level}] ${msg}`;
    console.log(line);
    const logs = [...this.data.debugLogs, { level, text: line }];
    if (logs.length > 100) logs.shift();
    this.setData({ debugLogs: logs });
  },

  toggleDebug() {
    this.setData({ showDebug: !this.data.showDebug });
  },

  onPullDownRefresh() {
    this.loadFiles().finally(() => wx.stopPullDownRefresh());
  },

  async loadFiles() {
    this.setData({ loading: true });
    try {
      this.addDebugLog('INFO', '开始获取文件列表...');
      const files = await recorder.getFileList();
      this.addDebugLog('INFO', `获取到 ${files.length} 个文件`);
      files.sort((a, b) => (a.name > b.name ? -1 : 1));
      const processed = files.map(f => ({
        ...f,
        durationText: this.formatDuration(f.duration),
        sizeText: this.formatSize(f.size),
      }));
      this.setData({ files: processed, loading: false });
      if (files.length === 0) {
        wx.showToast({ title: '设备暂无录音', icon: 'none' });
      } else {
        wx.showToast({ title: `共 ${files.length} 条录音`, icon: 'none' });
      }
    } catch (e) {
      console.error(`[Files] 文件列表获取失败:`, e);
      this.addDebugLog('ERR', `文件列表获取失败: ${e.message || e}`);
      this.setData({ loading: false });
      wx.showToast({ title: `加载失败: ${e.message}`, icon: 'none' });
    }
  },

  async onDownload(e) {
    const idx = e.currentTarget.dataset.index;
    const entry = this.data.files[idx];
    if (!entry || this.data.downloading) return;

    this.setData({ downloading: true, downloadProgress: '准备下载...' });
    recorder.onDownloadProgress = (received) => {
      const kb = (received / 1024).toFixed(0);
      this.setData({ downloadProgress: `下载中... ${kb} KB` });
    };

    try {
      const result = await recorder.download(entry);
      // 保存到小程序文件系统
      const fs = wx.getFileSystemManager();
      const fileName = result.name || entry.name;
      const savePath = `${wx.env.USER_DATA_PATH}/${fileName}`;
      fs.writeFileSync(savePath, result.data, 'binary');
      // 记录已下载
      const dl = {
        name: fileName,
        path: savePath,
        size: result.data.byteLength,
        sizeText: this.formatSize(result.data.byteLength),
        time: new Date().toLocaleString(),
        isWav: result.isWav,
        typeText: result.isWav ? 'WAV' : 'OPUS',
      };
      const downloadedFiles = [dl, ...this.data.downloadedFiles];
      this.setData({ downloadedFiles, downloading: false, downloadProgress: '' });
      wx.showToast({ title: '下载完成', icon: 'success' });
      // 提示是否转写
      this.askTranscribe(savePath, fileName, result.isWav);
    } catch (e) {
      this.setData({ downloading: false, downloadProgress: '' });
      const msg = e instanceof RecorderError ? e.message : (e.errMsg || e.message);
      wx.showToast({ title: `下载失败: ${msg}`, icon: 'none', duration: 3000 });
    }
  },

  askTranscribe(path, name, isWav) {
    wx.showModal({
      title: '下载完成',
      content: `文件 ${name} 已保存。\n是否立即转写成文字？`,
      confirmText: '转写',
      cancelText: '稍后',
      success: (res) => {
        if (res.confirm) {
          // 跳转到实时转写页的"文件转写"模式
          wx.navigateTo({
            url: `/pages/transcribe/transcribe?mode=file&path=${encodeURIComponent(path)}&name=${encodeURIComponent(name)}`,
          });
        }
      },
    });
  },

  async onDelete(e) {
    const idx = e.currentTarget.dataset.index;
    const entry = this.data.files[idx];
    if (!entry) return;
    wx.showModal({
      title: '删除录音',
      content: `确定删除 ${entry.name}？`,
      dangerColor: '#e74c3c',
      success: async (res) => {
        if (!res.confirm) return;
        try {
          await recorder.deleteFile(entry);
          const files = this.data.files.filter((_, i) => i !== idx);
          this.setData({ files });
          wx.showToast({ title: '已删除', icon: 'success' });
        } catch (e) {
          wx.showToast({ title: `删除失败: ${e.message}`, icon: 'none' });
        }
      },
    });
  },

  onPlayDownloaded(e) {
    const idx = e.currentTarget.dataset.index;
    const file = this.data.downloadedFiles[idx];
    if (!file) return;
    // 小程序播放本地音频
    const audio = wx.createInnerAudioContext();
    audio.src = file.path;
    audio.play();
    wx.showToast({ title: `播放: ${file.name}`, icon: 'none' });
    audio.onError((err) => {
      wx.showToast({ title: `播放失败: ${err.errMsg}`, icon: 'none' });
    });
  },

  onShareDownloaded(e) {
    const idx = e.currentTarget.dataset.index;
    const file = this.data.downloadedFiles[idx];
    if (!file) return;
    wx.shareFileMessage({
      filePath: file.path,
      fileName: file.name,
      success: () => {},
      fail: (err) => {
        wx.showToast({ title: '分享取消', icon: 'none' });
      },
    });
  },

  formatDuration(sec) {
    if (sec == null) return '0:00';
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) {
      return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    }
    return `${m}:${s.toString().padStart(2, '0')}`;
  },

  formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1048576).toFixed(1)} MB`;
  },
});
