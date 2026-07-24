/**
 * Mineflayer 4.x + MC 1.21.1 chat 协议兼容补丁
 *
 * bot._client.chat() 在 minecraft-protocol 1.66+ 中已被移除
 * 使用 chat_message / chat_command 数据包替代
 *
 * 用法: require('./chat_patch_1_21.js')(bot)
 */
module.exports = function patchChat(bot) {
  // 1) 静默 physicTick 弃用警告 (mineflayer-pvp 插件使用旧事件名)
  (function () {
    var _origEmit = bot.constructor.prototype.emit
    bot.constructor.prototype.emit = function (eventName) {
      if (eventName === 'physicTick') {
        var _warn = console.warn
        console.warn = function () {
          if (
            arguments[0] &&
            typeof arguments[0] === 'string' &&
            arguments[0].indexOf('physicTick') !== -1
          ) {
            return
          }
          return _warn.apply(console, arguments)
        }
        var result = _origEmit.apply(this, arguments)
        console.warn = _warn
        return result
      }
      return _origEmit.apply(this, arguments)
    }
  })()

  // 2) 补丁 bot._client.chat (MC 1.21.1 使用新聊天协议)
  ;(function () {
    if (typeof bot._client.chat !== 'function') {
      bot._client.chat = function (message) {
        message = String(message)
        if (message.startsWith('/')) {
          bot._client.write('chat_command', { command: message.substring(1) })
        } else {
          try {
            bot._client.write('chat_message', {
              message: message,
              timestamp: BigInt(Date.now()),
              salt: 0n,
              signature: null,
              offset: 0,
              acknowledged: Buffer.alloc(3, 0),
            })
          } catch (e) {
            bot._client.write('chat_command', { command: message })
          }
        }
      }
    }
  })()
}
