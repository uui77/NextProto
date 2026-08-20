// utils/ble.js — BLE 传输层：扫描、连接、MTU、Notify 订阅、AE21 写入
// 对应 Python 版 recorder/ble.py
const P = require('./protocol.js');

// QS668 BLE GATT UUID
const SERVICE_UUID = '0000AE20-0000-1000-8000-00805F9B34FB';
const CHAR_WRITE = '0000AE21-0000-1000-8000-00805F9B34FB';
const CHAR_NOTIFY_CTRL = '0000AE22-0000-1000-8000-00805F9B34FB';
const CHAR_NOTIFY_KEY = '0000AE23-0000-1000-8000-00805F9B34FB';

const SCAN_TIMEOUT_MS = 6000;

function isTargetDevice(device) {
  const name = (device.name || device.localName || '').toUpperCase();
  if (name.includes('QS668') || name.includes('QS68')) return true;
  if (device.advertisServiceUUIDs) {
    for (const uuid of device.advertisServiceUUIDs) {
      if (uuid.toUpperCase().includes('AE20')) return true;
    }
  }
  return false;
}

class BleTransport {
  constructor() {
    this.deviceId = null;
    this.connected = false;
    this.mtu = 23;
    // 实际发现的服务 UUID（设备可能返回大小写/格式不同的 UUID）
    this._serviceUuid = null;
    // 实际发现的写特征 UUID
    this._writeCharUuid = null;
    // 写特征属性（用于判断是否支持 writeNoResponse）
    this._writeProps = null;
    // 两个独立解析器
    this.parserCtrl = new P.FrameParser('AE22');
    this.parserKey = new P.FrameParser('AE23');
    // 帧回调
    this.onFrame = null;
    this.onDisconnect = null;
    this.onStateChange = null;
    this._notifyRegistered = false;
  }

  scan(timeout = SCAN_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      const found = [];
      wx.startBluetoothDevicesDiscovery({
        allowDuplicatesKey: false,
        success: () => {
          console.log('[BLE] 扫描开始');
          const handler = (res) => {
            const dev = res.devices && res.devices[0];
            if (dev && isTargetDevice(dev) && !found.find(d => d.deviceId === dev.deviceId)) {
              found.push(dev);
            }
          };
          wx.onBluetoothDeviceFound(handler);
          setTimeout(() => {
            wx.stopBluetoothDevicesDiscovery({
              success: () => {
                wx.offBluetoothDeviceFound(handler);
                console.log(`[BLE] 扫描结束，找到 ${found.length} 个目标设备`);
                resolve(found);
              },
              fail: () => resolve(found),
            });
          }, timeout);
        },
        fail: (err) => {
          console.error('[BLE] 扫描失败', err);
          reject(err);
        },
      });
    });
  }

  scanAll(timeout = SCAN_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      const found = [];
      wx.startBluetoothDevicesDiscovery({
        allowDuplicatesKey: false,
        success: () => {
          const handler = (res) => {
            for (const dev of (res.devices || [])) {
              if (!found.find(d => d.deviceId === dev.deviceId)) {
                found.push(dev);
              }
            }
          };
          wx.onBluetoothDeviceFound(handler);
          setTimeout(() => {
            wx.stopBluetoothDevicesDiscovery({
              success: () => {
                wx.offBluetoothDeviceFound(handler);
                resolve(found);
              },
              fail: () => resolve(found),
            });
          }, timeout);
        },
        fail: (err) => reject(err),
      });
    });
  }

  stopScan() {
    return new Promise((resolve) => {
      wx.stopBluetoothDevicesDiscovery({ success: resolve, fail: resolve });
    });
  }

  connect(deviceId) {
    return new Promise((resolve, reject) => {
      console.log('[BLE] 连接设备:', deviceId);
      wx.createBLEConnection({
        deviceId,
        timeout: 15000,
        success: () => {
          this.deviceId = deviceId;
          this.connected = true;
          console.log('[BLE] 连接成功');
          this._setupServices().then(resolve).catch(reject);
        },
        fail: (err) => {
          console.error('[BLE] 连接失败', err);
          reject(err);
        },
      });
    });
  }

  async _setupServices() {
    // 0. 先注册全局 Notify 回调（必须在订阅之前注册，否则可能丢失首包）
    if (!this._notifyRegistered) {
      this._notifyRegistered = true;
      wx.onBLECharacteristicValueChange((res) => {
        this._handleNotify(res);
      });
      console.log('[BLE] 全局 Notify 回调已注册');
    }

    // 1. 获取服务
    const services = await this._getServices();
    console.log('[BLE] 发现服务:', services.map(s => s.uuid));
    const targetService = services.find(s =>
      s.uuid.toUpperCase().includes('AE20'));
    if (!targetService) throw new Error('未找到 AE20 服务');
    this._serviceUuid = targetService.uuid;
    console.log('[BLE] 目标服务 UUID:', this._serviceUuid);

    // 2. 获取特征值
    const chars = await this._getCharacteristics(this._serviceUuid);
    console.log('[BLE] 发现特征:', chars.map(c => c.uuid));
    let notifyCtrlChar = null, notifyKeyChar = null, writeChar = null;
    for (const ch of chars) {
      const uuid = ch.uuid.toUpperCase();
      if (uuid.includes('AE21')) writeChar = ch;
      if (uuid.includes('AE22')) notifyCtrlChar = ch;
      if (uuid.includes('AE23')) notifyKeyChar = ch;
    }
    if (!writeChar) throw new Error('未找到 AE21 写特征');
    if (!notifyCtrlChar) throw new Error('未找到 AE22 通知特征');
    // 存储实际写特征 UUID（避免设备 UUID 格式差异导致写入失败）
    this._writeCharUuid = writeChar.uuid;
    console.log('[BLE] 写特征 UUID:', this._writeCharUuid);

    // 3. 确认特征具备通知/写权限，存储写特征属性供写入时判断
    const ctrlProps = notifyCtrlChar.properties || {};
    const keyProps = notifyKeyChar ? (notifyKeyChar.properties || {}) : {};
    this._writeProps = writeChar.properties || {};
    console.log(`[BLE] AE22 特征属性: ${JSON.stringify(ctrlProps)}`);
    console.log(`[BLE] AE23 特征属性: ${JSON.stringify(keyProps)}`);
    console.log(`[BLE] AE21 写特征属性: ${JSON.stringify(this._writeProps)}`);
    const canNoResp = !!this._writeProps.writeNoResponse;
    const canWrite = !!this._writeProps.write;
    console.log(`[BLE] AE21 写支持: write=${canWrite}, writeNoResponse=${canNoResp}`);

    // 4. 订阅 AE22（控制+数据通道）
    await this._subscribeNotify(notifyCtrlChar.uuid);
    // 5. 订阅 AE23（按键事件通道，可选）
    if (notifyKeyChar) {
      try { await this._subscribeNotify(notifyKeyChar.uuid); } catch (e) {
        console.warn('[BLE] AE23 订阅失败（非致命）');
      }
    } else {
      console.warn('[BLE] 未发现 AE23 按键特征，设备按键事件将不可用');
    }

    // 6. 协商 MTU（部分设备不支持会失败，降级为 23）
    try {
      const mtuRes = await this._setMTU(247);
      this.mtu = mtuRes || 23;
    } catch (e) { this.mtu = 23; }

    console.log(`[BLE] 服务初始化完成，MTU=${this.mtu}`);
  }

  _getServices() {
    return new Promise((resolve, reject) => {
      wx.getBLEDeviceServices({
        deviceId: this.deviceId,
        success: (res) => resolve(res.services),
        fail: reject,
      });
    });
  }

  _getCharacteristics(serviceId) {
    return new Promise((resolve, reject) => {
      wx.getBLEDeviceCharacteristics({
        deviceId: this.deviceId,
        serviceId,
        success: (res) => resolve(res.characteristics),
        fail: reject,
      });
    });
  }

  _subscribeNotify(charUuid) {
    return new Promise((resolve, reject) => {
      wx.notifyBLECharacteristicValueChange({
        deviceId: this.deviceId,
        serviceId: this._serviceUuid,
        characteristicId: charUuid,
        state: true,
        success: () => {
          console.log(`[BLE] 订阅 ${charUuid.slice(-8)} 成功`);
          resolve();
        },
        fail: (err) => {
          console.error(`[BLE] 订阅 ${charUuid.slice(-8)} 失败`, err);
          reject(err);
        },
      });
    });
  }

  _setMTU(mtu) {
    return new Promise((resolve, reject) => {
      wx.setBLEMTU({
        deviceId: this.deviceId,
        mtu,
        success: (res) => resolve(res.mtu),
        fail: reject,
      });
    });
  }

  _handleNotify(res) {
    if (!res || !res.characteristicId || !res.value) return;
    const charUuid = res.characteristicId.toUpperCase();
    const bytes = new Uint8Array(res.value);
    let parser, source;
    if (charUuid.includes('AE22')) {
      parser = this.parserCtrl; source = 'AE22';
    } else if (charUuid.includes('AE23')) {
      parser = this.parserKey; source = 'AE23';
    } else {
      console.log(`[BLE] 收到非预期特征 ${charUuid} ${bytes.length}B`);
      return;
    }
    // 每次通知打印前 20 字节 hex，便于定位
    const hexPreview = Array.from(bytes.slice(0, 20))
      .map(b => b.toString(16).padStart(2, '0')).join(' ');
    console.log(`[BLE←] ${source} ${bytes.length}B hex=${hexPreview}${bytes.length > 20 ? '...' : ''}`);

    const frames = parser.feed(res.value);
    if (frames.length === 0) {
      console.log(`[BLE] ${source} 未解析出完整帧（累积 ${parser._buf.length} 字节缓冲，CRC 错误 ${parser.crcErrors}）`);
      return;
    }
    for (const frame of frames) {
      console.log(`[BLE→] ${source} 帧 seq=${frame.seq} type=${frame.type} cmd=${frame.cmd} bodyLen=${frame.body.length}`);
      if (this.onFrame) {
        try {
          this.onFrame(frame, source);
        } catch (e) {
          console.error('[BLE] onFrame 回调异常', e);
        }
      }
    }
  }

  write(data) {
    return new Promise((resolve, reject) => {
      const hex = Array.from(new Uint8Array(data)).slice(0, 20)
        .map(b => b.toString(16).padStart(2, '0')).join(' ');
      const useNoResp = this._writeProps && this._writeProps.writeNoResponse;
      const firstType = useNoResp ? 'noResponse' : 'default';
      const fallbackType = useNoResp ? 'default' : 'noResponse';
      const deviceId = this.deviceId;
      const serviceId = this._serviceUuid || SERVICE_UUID;
      const charId = this._writeCharUuid || CHAR_WRITE;
      console.log(`[BLE→] 写入 ${data.byteLength}B hex=${hex}${data.byteLength > 20 ? '...' : ''} writeType=${firstType}`);

      const doWrite = (wtype, isFallback = false) => {
        const opts = {
          deviceId,
          serviceId,
          characteristicId: charId,
          value: data,
          success: () => {
            console.log(`[BLE] 写入成功 (${wtype})`);
            resolve();
          },
          fail: (err) => {
            if (isFallback) {
              // 两种类型都失败，最后尝试不带 writeType（兼容旧版微信）
              console.warn('[BLE] 两种写入类型均失败，尝试不带 writeType...');
              wx.writeBLECharacteristicValue({
                deviceId, serviceId, characteristicId: charId, value: data,
                success: () => { console.log('[BLE] 写入成功 (无类型)'); resolve(); },
                fail: (err2) => { console.error('[BLE] 写入失败', err2); reject(err2); },
              });
            } else {
              console.warn(`[BLE] writeType=${wtype} 失败，尝试 ${fallbackType}...`);
              doWrite(fallbackType, true);
            }
          },
        };
        // 仅在新版微信中传递 writeType
        if (wtype) opts.writeType = wtype;
        wx.writeBLECharacteristicValue(opts);
      };
      doWrite(firstType);
    });
  }

  disconnect() {
    if (!this.deviceId) return Promise.resolve();
    return new Promise((resolve) => {
      wx.closeBLEConnection({
        deviceId: this.deviceId,
        success: () => {
          console.log('[BLE] 已断开');
          this.connected = false;
          this.deviceId = null;
          this._serviceUuid = null;
          this._writeCharUuid = null;
          this._writeProps = null;
          this.parserCtrl.reset();
          this.parserKey.reset();
          if (this.onDisconnect) this.onDisconnect();
          if (this.onStateChange) this.onStateChange('disconnected');
          resolve();
        },
        fail: () => resolve(),
      });
    });
  }

  monitorConnectionState() {
    wx.onBLEConnectionStateChange((res) => {
      if (res.deviceId === this.deviceId && !res.connected) {
        console.log('[BLE] 设备连接断开');
        this.connected = false;
        if (this.onDisconnect) this.onDisconnect();
        if (this.onStateChange) this.onStateChange('disconnected');
      }
    });
  }
}

module.exports = {
  BleTransport,
  SERVICE_UUID,
  CHAR_WRITE,
  CHAR_NOTIFY_CTRL,
  CHAR_NOTIFY_KEY,
  isTargetDevice,
};
