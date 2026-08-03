require 'json'

webhook_url = ARGV.fetch(0)
secret_output = ARGV.fetch(1)
config = JSON.parse(File.read(ARGV.fetch(2), encoding: 'UTF-8'))
company = config.fetch('company')
assistant = config.fetch('assistant')
chatwoot = config.fetch('chatwoot')
rotate_webhook_secret = ENV.fetch('ROTATE_CHATWOOT_WEBHOOK_SECRET', '0') == '1'

account = Account.order(:created_at).first
raise 'Chatwoot has no initialized account.' unless account

administrator = account.users.order(:created_at).first
raise 'Chatwoot account has no administrator.' unless administrator

account.update!(name: company.fetch('name')) unless account.name == company.fetch('name')

inbox = account.inboxes.find_by(
  name: chatwoot.fetch('inbox_name'),
  channel_type: 'Channel::WebWidget'
)
inbox ||= account.inboxes.where(channel_type: 'Channel::WebWidget').order(:created_at).first
unless inbox
  ActiveRecord::Base.transaction do
    channel = account.web_widgets.create!(
      website_url: company.fetch('website_url'),
      widget_color: chatwoot.fetch('widget_color'),
      welcome_title: assistant.fetch('welcome_title'),
      welcome_tagline: assistant.fetch('welcome_message')
    )
    inbox = account.inboxes.create!(
      name: chatwoot.fetch('inbox_name'),
      channel: channel,
      greeting_enabled: true,
      greeting_message: assistant.fetch('welcome_message')
    )
    InboxMember.find_or_create_by!(inbox: inbox, user: administrator)
  end
end

inbox.channel.update!(
  website_url: company.fetch('website_url'),
  widget_color: chatwoot.fetch('widget_color'),
  welcome_title: assistant.fetch('welcome_title'),
  welcome_tagline: assistant.fetch('welcome_message')
)
inbox.update!(
  name: chatwoot.fetch('inbox_name'),
  greeting_enabled: true,
  greeting_message: assistant.fetch('welcome_message')
)
InboxMember.find_or_create_by!(inbox: inbox, user: administrator)

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
