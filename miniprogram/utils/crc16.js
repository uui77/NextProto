// utils/crc16.js — CRC-16/XMODEM
// 协议 3.1 节：Poly=0x1021, Init=0x0000, RefIn/RefOut=false, XorOut=0x0000
// 检验向量: ASCII "123456789" -> 0x31C3

// 预生成 256 项查表
const TABLE = new Uint16Array(256);
for (let byte = 0; byte < 256; byte++) {
  let crc = byte << 8;
  for (let i = 0; i < 8; i++) {
    if (crc & 0x8000) {
      crc = ((crc << 1) ^ 0x1021) & 0xFFFF;
    } else {
      crc = (crc << 1) & 0xFFFF;
    }
  }
  TABLE[byte] = crc;
}

/**
 * 计算 CRC-16/XMODEM
 * @param {ArrayBuffer|Uint8Array} data — 输入数据
 * @param {number} [crc=0] — 初始值
 * @returns {number} CRC-16 校验值
 */
function crc16Xmodem(data, crc = 0x0000) {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  for (let i = 0; i < bytes.length; i++) {
    crc = ((crc << 8) & 0xFFFF) ^ TABLE[((crc >> 8) ^ bytes[i]) & 0xFF];
  }
  return crc;
}

module.exports = { crc16Xmodem, TABLE };
