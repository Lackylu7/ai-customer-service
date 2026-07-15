require 'json'

webhook_url = ARGV.fetch(0)
secret_output = ARGV.fetch(1)
rotate_webhook_secret = ENV.fetch('ROTATE_CHATWOOT_WEBHOOK_SECRET', '0') == '1'

account = Account.order(:created_at).first
raise 'Chatwoot has no initialized account.' unless account

administrator = account.users.order(:created_at).first
raise 'Chatwoot account has no administrator.' unless administrator

account.update!(name: '星河优选') unless account.name == '星河优选'

inbox = account.inboxes.find_by(
  name: '星河优选在线客服',
  channel_type: 'Channel::WebWidget'
)
unless inbox
  ActiveRecord::Base.transaction do
    channel = account.web_widgets.create!(
      website_url: 'http://127.0.0.1:8000',
      widget_color: '#2563eb',
      welcome_title: '星河优选在线客服',
      welcome_tagline: '商品、配送与售后问题都可以在这里咨询。'
    )
    inbox = account.inboxes.create!(
      name: '星河优选在线客服',
      channel: channel,
      greeting_enabled: true,
      greeting_message: '您好，我是星河优选智能客服，请问有什么可以帮您？'
    )
    InboxMember.find_or_create_by!(inbox: inbox, user: administrator)
  end
end

webhook = account.webhooks.find_by(name: 'AI Customer Service')
if webhook
  webhook.update!(url: webhook_url, subscriptions: ['message_created'])
else
  webhook = account.webhooks.create!(
    name: 'AI Customer Service',
    url: webhook_url,
    subscriptions: ['message_created']
  )
end
webhook.reset_secret! if rotate_webhook_secret

account.labels.find_or_create_by!(title: 'ai_handoff') do |label|
  label.color = '#f59e0b'
  label.description = 'AI 已停止回复，等待人工客服接管。'
  label.show_on_sidebar = true
end

access_token = administrator.access_token || administrator.create_access_token!
payload = {
  account_id: account.id,
  inbox_id: inbox.id,
  api_token: access_token.token,
  webhook_secret: webhook.secret,
  website_token: inbox.channel.website_token
}
File.write(secret_output, JSON.generate(payload))
File.chmod(0o600, secret_output)

puts JSON.generate({ account_id: account.id, inbox_id: inbox.id, status: 'configured' })
