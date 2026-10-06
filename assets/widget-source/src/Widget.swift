import Cocoa
import SwiftUI
import CoreText
import ApplicationServices

struct WindowQuota: Decodable { var usedPercent: Double; var windowDurationMins: Int; var resetsAt: Double? }
struct Credits:Decodable { var hasCredits:Bool?; var unlimited:Bool?; var balance:String? }
struct Bucket: Decodable, Identifiable {
 var id:String; var name:String; var primary:WindowQuota?; var secondary:WindowQuota?; var planType:String?; var planLabel:String?; var credits:Credits?
 var displayPlan:String {planLabel ?? planType?.uppercased() ?? "套餐未知"}
}
struct CompletedRun:Decodable,Identifiable { var id:String; var usage:[String:Int]; var status:String; var ended:String }
struct TaskUsage: Decodable, Identifiable { var id:String; var title:String; var total:[String:Int]; var turn:[String:Int]; var status:String; var modified:Double; var warning:Bool; var history:[CompletedRun] }
struct Run:Decodable,Identifiable { var id:String; var title:String; var sent:Double; var started:Double; var ended:Double?; var duration:Double?; var status:String; var usage:[String:Int]; var model:String; var effort:String; var merged:Bool; var estimate:String?; var grouped:Bool?; var ownUsage:[String:Int]?; var children:[Run]?; var agentCount:Int?; var partialUsage:Bool?; var ownStatus:String? }
struct RadarData:Decodable { var headline:String; var details:[String]; var updated:Double?; var resetReference:String? }
struct ResetHistory:Decodable,Identifiable { var id:String; var started:Double; var confirmed:Double?; var outcome:String }
struct ResetCardsState:Decodable { var availableCount:Int?; var account:String?; var pending:Bool?; var busy:Bool?; var command:String?; var message:String?; var history:[ResetHistory] }
struct Snapshot: Decodable { var radar:RadarData?; var checked:Double; var quotaUpdated:Double?; var quotaError:String?; var buckets:[Bucket]; var tasks:[TaskUsage]; var runs:[Run]; var groups:[Run]?; var resetCards:ResetCardsState? }
func number(_ n:Int) -> String {
 if n < 10_000 {return String(n)}
 let scale:Double = n >= 100_000_000 ? 100_000_000 : 10_000
 let label = n >= 100_000_000 ? "亿" : "万"
 var text=String(format:"%.1f",Double(n)/scale)
 if text.hasSuffix(".0"){text.removeLast(2)}
 return text+label
}
func tokenText(_ d:[String:Int])->String { guard let n=d["total_tokens"] else{return "等待统计"};return number(n)+" Token" }
func turnTokenLabel(_ d:[String:Int])->Text {
 guard let n=d["total_tokens"] else{return Text("本轮 等待统计")}
 let formatted=number(n)
 let unit=formatted.hasSuffix("万") ? "万":formatted.hasSuffix("亿") ? "亿":""
 let digits=unit.isEmpty ? formatted:String(formatted.dropLast())
 return Text("本轮 \(Text(digits).fontWeight(.semibold))\(unit) Token")
}
func modelDisplayName(_ raw:String)->String {
 guard raw.lowercased().hasPrefix("gpt-") else{return raw}
 let names=["sol":"Sol","astra":"Astra","terra":"Terra","luna":"Luna","codex":"Codex"]
 let parts=String(raw.dropFirst(4)).split(separator:"-").map(String.init)
 return "GPT-"+parts.map{names[$0.lowercased()] ?? $0}.joined(separator:" ")
}
func effortDisplayName(_ raw:String)->String {
 let names=["none":"无","minimal":"最低","low":"轻度","medium":"中","high":"高","xhigh":"极高","max":"Max","ultra":"Ultra","persistent":"持续"]
 return names[raw.lowercased()] ?? raw
}
// sRGB sampled from the user's reference screenshot; only the effort label is tinted
let ultraEffortColor=Color(.sRGB,red:174.0/255,green:123.0/255,blue:249.0/255,opacity:1)
func modelLineText(model:String,effort:String)->AttributedString {
 var text=AttributedString("模型："+modelDisplayName(model)+" ")
 var label=AttributedString(effortDisplayName(effort))
 if effort.lowercased() == "ultra" {label.foregroundColor=ultraEffortColor}
 text.append(label)
 return text
}
func creditsText(_ credits:Credits?)->String {
 guard let credits=credits else{return "暂未获取"}
 if credits.unlimited == true {return "不限量"}
 guard let raw=credits.balance,let amount=Decimal(string:raw,locale:Locale(identifier:"en_US_POSIX")) else{return "暂未获取"}
 let formatter=NumberFormatter();formatter.numberStyle = .decimal;formatter.locale=Locale(identifier:"en_US")
 formatter.maximumFractionDigits=8
 return (formatter.string(from:NSDecimalNumber(decimal:amount)) ?? raw)+" 点"
}
private let fullPanelHeight:CGFloat=446
private let defaultPanelWidth:CGFloat=300
private let minimumPanelHeight:CGFloat=240
private let panelControlInset:CGFloat=8
private let panelControlSize:CGFloat=24
func dateText(_ n:Double?) -> String { guard let n=n else{return "等待数据"};let f=DateFormatter();f.dateFormat="M月d日 HH:mm";return f.string(from:Date(timeIntervalSince1970:n)) }
private let quotaNumberFont=NSFont(descriptor:NSFont.systemFont(ofSize:35,weight:.semibold).fontDescriptor.withDesign(.rounded)!,size:35)!
private let quotaLabelFont=NSFont.systemFont(ofSize:13)
private func textInkAscent(_ text:String,font:NSFont)->CGFloat {
 let line=CTLineCreateWithAttributedString(NSAttributedString(string:text,attributes:[.font:font]))
 return CTLineGetBoundsWithOptions(line,.useGlyphPathBounds).maxY
}

final class Model:ObservableObject {
 @Published var data:Snapshot?; @Published var minimized=false; @Published var error:String?; var process:Process?; var buffer=Data()
 private let decodeQueue=DispatchQueue(label:"usage-widget.decode",qos:.utility)
 @Published var compact=false
 @Published var cardPending=false
 @Published var cardNotice:String?
 var interactionActive=false
 private var pendingSnapshot:Snapshot?
 private var input:Pipe?;private var cardRequest:String?
 var canUseCard:Bool {
  guard process?.isRunning == true,let data=data,data.quotaError == nil,let updated=data.quotaUpdated,Date().timeIntervalSince1970-updated<120,let cards=data.resetCards,cards.account != nil else{return false}
  return !cardPending && cards.busy != true && ((cards.availableCount ?? 0)>0 || cards.pending == true)
 }
 func consumeCard(){
  guard canUseCard,let account=data?.resetCards?.account else{return}
  let id=UUID().uuidString
  guard let bytes=try? JSONSerialization.data(withJSONObject:["action":"consumeReset","confirmed":true,"account":account,"requestId":id]) else{return}
  cardPending=true;cardRequest=id;cardNotice=nil
  do{try input?.fileHandleForWriting.write(contentsOf:bytes+Data([10]))}catch{cardPending=false;cardNotice="发送失败，未确认使用结果"}
 }
 func start(){
  guard let script=Bundle.main.path(forResource:"backend",ofType:"py") else {error="缺少统计程序";return}
  let runtime=Bundle.main.path(forResource:"python-runtime",ofType:"txt").flatMap{try? String(contentsOfFile:$0,encoding:.utf8)}?.trimmingCharacters(in:.whitespacesAndNewlines) ?? "/usr/bin/python3"
  let p=Process();p.executableURL=URL(fileURLWithPath:runtime);p.arguments=[script];let pipe=Pipe();p.standardOutput=pipe;p.standardError=FileHandle.nullDevice
  let commands=Pipe();p.standardInput=commands;input=commands
  pipe.fileHandleForReading.readabilityHandler={ [weak self] handle in
   let chunk=handle.availableData
   guard !chunk.isEmpty else {handle.readabilityHandler=nil;return}
   self?.decodeQueue.async {
    guard let self=self else{return};self.buffer.append(chunk)
    var latest:Snapshot?
    while let range=self.buffer.range(of:Data([10])) {
     let line=self.buffer.subdata(in:0..<range.lowerBound);self.buffer.removeSubrange(0..<range.upperBound)
     if var s=try? JSONDecoder().decode(Snapshot.self,from:line){
      if let groups=s.groups {s.groups=historyRuns(groups)}
      latest=s
     }
    }
    if let s=latest {DispatchQueue.main.async {self.receive(s)}}
   }
  }
  p.terminationHandler={ [weak self] _ in DispatchQueue.main.async {self?.error="统计进程已停止，请重新打开浮窗";self?.cardPending=false} }
  do{try p.run();process=p}catch{self.error="无法启动本机统计程序"}
 }
 func stop(){process?.terminate()}
 func receive(_ snapshot:Snapshot){
  if interactionActive {pendingSnapshot=snapshot;return}
  data=snapshot;error=nil
  if let request=cardRequest,snapshot.resetCards?.command == request,snapshot.resetCards?.busy == false {
   cardPending=false;cardRequest=nil;cardNotice=snapshot.resetCards?.message
  }
 }
 func finishInteraction(){
  interactionActive=false
  if let snapshot=pendingSnapshot {pendingSnapshot=nil;receive(snapshot)}
 }
}
let runTokenColor=Color(red:0.36,green:0.62,blue:1.0)
private let finishedRunOpacity:Double=0.85
func historyRuns(_ runs:[Run])->[Run] {
 runs.sorted { lhs,rhs in
  let lhsRunning=lhs.status == "进行中",rhsRunning=rhs.status == "进行中"
  if lhsRunning != rhsRunning {return lhsRunning}
  if lhs.sent != rhs.sent {return lhs.sent > rhs.sent}
  return lhs.id > rhs.id
 }
}
func durationText(_ seconds:Double)->String {let n=max(0,Int(seconds));return n>=60 ? "\(n/60)分\(n%60)秒" : "\(n)秒"}
func runDurationText(_ run:Run,now:Double)->String {
 if let seconds=run.duration {return durationText(seconds)}
 if run.grouped == true && (run.started<=0 || (run.ended == nil && (run.ownStatus ?? run.status) != "进行中")) {return "耗时未知"}
 return durationText(max(0,(run.ended ?? now)-run.started))
}
func sentTimeText(_ stamp:Double)->String {
 guard stamp>0 else{return "时间未知"}
 let date=Date(timeIntervalSince1970:stamp);let f=DateFormatter()
 f.dateFormat=Calendar.current.isDateInToday(date) ? "HH:mm":"M/d HH:mm"
 return f.string(from:date)
}
func radarProbability(_ headline:String)->Int? {
 guard let range=headline.range(of:#"\b(?:100|[0-9]{1,2})%"#,options:.regularExpression) else{return nil}
 return Int(headline[range].dropLast())
}
func radarHeadlineText(_ headline:String)->Text {
 var styled=AttributedString(headline)
 if let value=radarProbability(headline),value>80,
    let range=styled.range(of:"\(value)%") {
  styled[range].foregroundColor=Color(red:0.96,green:0.66,blue:0.28)
  styled[range].font = .system(size:12,weight:.semibold)
 }
 return Text(styled)
}
private let taskRowFont=Font.system(size:12,weight:.light)
struct RunRow:View {
 let run:Run;let now:Double
 private static let modelIcon:NSImage? = Bundle.main.path(forResource:"OpenAIModel",ofType:"png").flatMap{NSImage(contentsOfFile:$0)}
 var body:some View {
  VStack(alignment:.leading,spacing:4){
   HStack(alignment:.center,spacing:6){
    Circle().fill(run.status == "进行中" ? Color.yellow : Color.gray).frame(width:7,height:7)
    Text(run.title).lineLimit(1).truncationMode(.tail).font(taskRowFont).help(run.title+(run.merged ? "\n本轮包含运行中追加的消息，日志未单列其用量":""))
   }
   HStack(spacing:6){
    HStack(alignment:.center,spacing:4){
     if let icon=Self.modelIcon {
      Image(nsImage:icon).resizable().renderingMode(.template).scaledToFit().frame(width:12,height:12).accessibilityHidden(true)
     } else {Image(systemName:"cpu").resizable().scaledToFit().frame(width:12,height:12).accessibilityHidden(true)}
     Text(modelLineText(model:run.model,effort:run.effort)).lineLimit(1).truncationMode(.tail)
    }.font(taskRowFont).foregroundStyle(Color(white:0.733).opacity(0.87))
     .help("模型："+modelDisplayName(run.model)+" "+effortDisplayName(run.effort))
    Spacer(minLength:0)
    Text(sentTimeText(run.sent)).font(taskRowFont).foregroundStyle(Color(white:0.733).opacity(0.87)).fixedSize().help("发起时间 "+dateText(run.sent))
   }.font(taskRowFont).foregroundStyle(Color(white:0.733))
   HStack(alignment:.center,spacing:4){
    HStack(spacing:4){
     Image(systemName:"timer").resizable().scaledToFit().frame(width:12,height:12).accessibilityHidden(true)
     ViewThatFits(in:.horizontal){
      Text((run.duration == nil && (run.ownStatus ?? run.status) == "进行中" ? "已进行：":"任务耗时：")+runDurationText(run,now:now)).fixedSize()
      Text(runDurationText(run,now:now)).fixedSize()
     }
    }.font(taskRowFont).foregroundStyle(Color(white:0.733).opacity(0.87)).lineLimit(1).truncationMode(.tail)
     .help("本轮主 Agent 耗时，不叠加并行子 Agent 时长；已结束的任务显示最终耗时")
    Spacer(minLength:0)
    turnTokenLabel(run.usage).font(taskRowFont).foregroundStyle(runTokenColor).lineLimit(1).fixedSize(horizontal:true,vertical:false)
     .help(run.partialUsage == true ? "本轮部分记录尚无用量，只汇总已知 tokens":"仅此条请求的主 Agent 与所属子 Agent 用量，含本轮实际处理的输入、缓存及输出 tokens")
   }
  }.frame(maxWidth:.infinity,alignment:.leading)
   .opacity(run.status == "进行中" ? 1:finishedRunOpacity)
 }
}
struct GroupRow:View {
 let run:Run;let now:Double
 @State private var expanded=false
 var body:some View {
  VStack(alignment:.leading,spacing:3){
   RunRow(run:run,now:now)
   if let children=run.children,!children.isEmpty {
    Button(action:{expanded.toggle()}){
     HStack(spacing:5){
      Image(systemName:expanded ? "chevron.down":"chevron.right").frame(width:10)
      Text("子 Agent \(run.agentCount ?? children.count)")
      Spacer()
      Text(expanded ? "收起":"展开")
     }.font(.system(size:12)).foregroundStyle(Color(red:0.49,green:0.65,blue:0.86))
      .padding(.vertical,3).contentShape(Rectangle())
    }.buttonStyle(.plain).accessibilityLabel(expanded ? "收起子 Agent":"展开子 Agent")
     .opacity(run.status == "进行中" ? 1:finishedRunOpacity)
    if expanded {
     VStack(alignment:.leading,spacing:10){
      Text("本轮自身："+tokenText(run.ownUsage ?? [:])).font(.system(size:12)).foregroundStyle(.secondary)
       .opacity(run.status == "进行中" ? 1:finishedRunOpacity)
      ForEach(children){child in AnyView(GroupRow(run:child,now:now))}
     }.padding(.leading,10).overlay(alignment:.leading){Rectangle().fill(Color.white.opacity(0.12)).frame(width:1).opacity(run.status == "进行中" ? 1:finishedRunOpacity)}
    }
   }
  }
 }
}
func updateTime(_ stamp:Double?)->String {
 guard let stamp=stamp else{return "等待数据"}
 let formatter=DateFormatter();formatter.dateFormat="HH:mm"
 return formatter.string(from:Date(timeIntervalSince1970:stamp))
}

struct ResetCardButtonStyle:ButtonStyle {
 @Environment(\.isEnabled) private var enabled
 func makeBody(configuration:Configuration)->some View {
  configuration.label.font(.system(size:12,weight:.medium))
   .padding(.horizontal,7).frame(minWidth:44,minHeight:22)
   .foregroundStyle(enabled ? Color.white.opacity(0.94):Color.white.opacity(0.32))
   .background(LinearGradient(colors:enabled ? [Color(red:0.24,green:0.46,blue:0.72),Color(red:0.14,green:0.29,blue:0.48)]:[Color.white.opacity(0.07),Color.white.opacity(0.035)],startPoint:.top,endPoint:.bottom))
   .clipShape(RoundedRectangle(cornerRadius:7))
   .overlay(RoundedRectangle(cornerRadius:7).stroke(enabled ? Color(red:0.45,green:0.66,blue:0.88).opacity(0.75):Color.white.opacity(0.16),lineWidth:1))
   .brightness(enabled && configuration.isPressed ? -0.07:0)
   .contentShape(RoundedRectangle(cornerRadius:7))
 }
}
struct BalanceRow:View {
 let credits:Credits?;let count:Int?;let enabled:Bool;let pending:Bool;let busy:Bool;let stale:Bool
 var use:()->Void
 var body:some View {
  GeometryReader { geometry in
  let narrow=geometry.size.width<266
  HStack(spacing:0){
   HStack(spacing:0){
    Text(narrow ? "积分 ":"剩余积分：").foregroundStyle(Color.secondary)
    Text(narrow ? creditsText(credits).replacingOccurrences(of:" 点",with:""):creditsText(credits)).monospacedDigit().foregroundStyle(stale ? Color.orange:Color.secondary)
   }.lineLimit(1).padding(.trailing,8).frame(width:geometry.size.width/2,alignment:.leading).help((stale ? "上次读取，当前刷新失败：":"剩余积分：")+creditsText(credits))
   HStack(spacing:0){
    Text((narrow ? "卡 ":"重置卡 ")+(count.map{narrow ? "\($0)":"\($0) 张"} ?? "未知")).foregroundStyle(stale ? Color.orange:Color.secondary).lineLimit(1)
     .help(count == nil ? "暂未获取卡数":"可用重置卡 \(count!) 张")
    Spacer(minLength:10)
    Button(busy ? "处理中":pending ? "重试":"使用",action:use)
     .buttonStyle(ResetCardButtonStyle()).disabled(!enabled)
     .help(pending ? "核对上次请求，可能完成原请求的扣卡；操作前再次确认":"使用重置卡，操作前再次确认；无卡或数据过期时不可使用")
   }.padding(.leading,10).frame(width:geometry.size.width/2,alignment:.leading)
  }.overlay {Rectangle().fill(Color.white.opacity(0.13)).frame(width:1,height:16).accessibilityHidden(true).allowsHitTesting(false)}
  }.frame(height:22).font(.system(size:12)).padding(.top,3)
 }
}
struct Card:View {
 @State private var radarOpen=false
 @State private var visibleCount=80
 @State private var confirmCard=false
 @ObservedObject var model:Model
 var minimize:()->Void
 var dismiss:()->Void
 var toggleShape:()->Void = {}
 var resetPosition:()->Void = {}
 var body:some View {
  GeometryReader { geometry in
  let narrow=geometry.size.width<290
  VStack(alignment:.leading,spacing:4){
   VStack(alignment:.leading,spacing:4){
   HStack{
    Circle().fill(model.data?.quotaError == nil && model.data?.quotaUpdated != nil ? Color.green : Color.orange).frame(width:7,height:7)
    Text("CODEX 用量").font(.system(size:11,weight:.bold,design:.rounded)).tracking(0.3).help("按住标题或额度区域可拖动浮窗，四边和四角可调整大小")
    if !narrow,let bucket=model.data?.buckets.first(where:{$0.id=="codex"}) {
     Text(bucket.displayPlan).font(.system(size:10,weight:.bold)).foregroundStyle(Color(white:0.733)).padding(.horizontal,6).padding(.vertical,3).background(Color.white.opacity(0.07)).clipShape(RoundedRectangle(cornerRadius:5))
      .help("套餐类型由账号读取；X 为本项目显示别名，不是官方实时额度倍率")
    }
    Spacer()
    HStack(spacing:4){
     Button(action:minimize){Image(systemName:"minus").frame(width:panelControlSize,height:panelControlSize).contentShape(Rectangle())}.buttonStyle(.plain).accessibilityLabel("最小化").help("最小化为可拖动横条")
     Button(action:toggleShape){Image(systemName:model.compact ? "arrow.up.left.and.arrow.down.right":"rectangle.compress.vertical").frame(width:panelControlSize,height:panelControlSize).contentShape(Rectangle())}.buttonStyle(.plain).accessibilityLabel(model.compact ? "展开任务列表":"收缩为第一条任务").help(model.compact ? "恢复完整任务列表":"只显示第一条任务")
     Button(action:dismiss){Image(systemName:"xmark").frame(width:panelControlSize,height:panelControlSize).contentShape(Rectangle())}.buttonStyle(.plain).accessibilityLabel("关闭浮窗").help("隐藏浮窗，可从菜单栏恢复")
    }
   }.foregroundStyle(.secondary)
   if let bucket=model.data?.buckets.first(where:{$0.id=="codex"}),let quota=bucket.primary {
    let remaining=String(format:"%.0f%%",max(0,min(100,100-quota.usedPercent)))
    let quotaLabel=quota.windowDurationMins>=10080 ? "每周额度":"账号额度"
    HStack(alignment:.top){
     Text(remaining).font(Font(quotaNumberFont)).monospacedDigit()
      .alignmentGuide(.top){$0[.firstTextBaseline]-textInkAscent(remaining,font:quotaNumberFont)}
     if !narrow {Text("剩余").foregroundStyle(.secondary).font(Font(quotaLabelFont))
      .alignmentGuide(.top){$0[.firstTextBaseline]-(textInkAscent(remaining,font:quotaNumberFont)+textInkAscent("剩余",font:quotaLabelFont))/2}
     }
     Spacer()
     VStack(alignment:.trailing,spacing:3){Text(quotaLabel).font(Font(quotaLabelFont)).foregroundStyle(Color.secondary);Text(dateText(quota.resetsAt)+" 重置").font(.system(size:12)).foregroundStyle(Color.secondary)}
      .alignmentGuide(.top){$0[.firstTextBaseline]-textInkAscent(quotaLabel,font:quotaLabelFont)}
    }
    .padding(.top,-8)
    ProgressView(value:max(0,min(100,100-quota.usedPercent)),total:100).tint(Color.white.opacity(0.55)).frame(height:8)
   } else {Text("正在读取账号额度…").font(.headline)}
   BalanceRow(credits:model.data?.buckets.first(where:{$0.id=="codex"})?.credits,count:model.data?.resetCards?.availableCount,enabled:model.canUseCard,pending:model.data?.resetCards?.pending == true,busy:model.cardPending,stale:model.data?.quotaError != nil,use:{confirmCard=true})
   }
   Button(action:{radarOpen.toggle()}) {
    HStack(spacing:4){
     Image(systemName:"antenna.radiowaves.left.and.right")
     radarHeadlineText(model.data?.radar?.headline ?? "24 小时重置雷达概率 · 等待数据").lineLimit(1)
     Spacer(minLength:0)
     Image(systemName:"chevron.right")
    }.font(.system(size:12)).foregroundStyle(Color(red:0.36,green:0.62,blue:1.0))
   }.buttonStyle(.plain).help("查看第三方公共预测、服务状态和重置记录")
    .popover(isPresented:$radarOpen,arrowEdge:.leading){
     ScrollView {
      VStack(alignment:.leading,spacing:10){
     Text("24 小时重置雷达概率").font(.headline)
       ForEach(Array((model.data?.radar?.details ?? ["等待数据"]).enumerated()),id:\.offset){_,line in
        Text(line).font(.system(size:13)).textSelection(.enabled).frame(maxWidth:.infinity,alignment:.leading)
       }
       Text("读取时间 "+updateTime(model.data?.radar?.updated)).font(.system(size:12)).foregroundStyle(.secondary)
      }.padding(16)
     }.frame(width:340,height:500).environment(\.colorScheme,.dark)
    }
   Rectangle().fill(Color.white.opacity(0.12)).frame(height:1).padding(.vertical,4).accessibilityHidden(true)
   if model.compact {
   if let run=(model.data?.groups ?? model.data?.runs ?? []).first {
    RunRow(run:run,now:model.data?.checked ?? Date().timeIntervalSince1970)
   } else {Text("等待本机处理记录").font(taskRowFont)}
   } else {
   ScrollView {
    LazyVStack(alignment:.leading,spacing:12){
     ForEach(Array((model.data?.groups ?? model.data?.runs ?? []).prefix(visibleCount))){run in
      GroupRow(run:run,now:model.data?.checked ?? Date().timeIntervalSince1970)
     }
     if (model.data?.groups ?? model.data?.runs)?.isEmpty != false {Text("等待本机处理记录").font(.system(size:12))}
     if (model.data?.groups ?? model.data?.runs ?? []).count>visibleCount {
      Button("加载更多历史记录"){visibleCount+=80}.buttonStyle(.plain).font(.system(size:12)).foregroundStyle(Color.blue).padding(.vertical,6)
     }
    }.padding(.trailing,3)
   }.frame(maxHeight:.infinity)
   }
   if model.compact {Spacer(minLength:0)}
   HStack(alignment:.center,spacing:6){
    Text(model.error ?? model.data?.quotaError ?? ("额度更新 "+updateTime(model.data?.quotaUpdated))).font(.system(size:11)).foregroundStyle(model.error != nil || model.data?.quotaError != nil ? Color.orange : Color.secondary)
     .help(model.error ?? model.data?.quotaError ?? ("额度更新 "+dateText(model.data?.quotaUpdated)))
    Spacer(minLength:0)
    Button(action:resetPosition){
     Image(systemName:"arrow.down.right.square").font(.system(size:13))
      .foregroundStyle(Color.secondary).frame(width:panelControlSize,height:panelControlSize)
      .contentShape(Rectangle())
    }.buttonStyle(.plain).accessibilityLabel("回到默认位置")
     .help("回到 Codex 窗口右下角，保留当前大小")
   }.frame(height:panelControlSize).lineLimit(1).truncationMode(.tail).padding(.horizontal,8)
  }.foregroundStyle(Color(white:0.733)).padding(.horizontal,12).padding(.top,panelControlInset).padding(.bottom,panelControlInset).frame(width:geometry.size.width,height:geometry.size.height,alignment:.topLeading)
   .background(Color(red:0.155,green:0.155,blue:0.155)).environment(\.colorScheme,.dark).clipShape(RoundedRectangle(cornerRadius:16))
   .overlay(RoundedRectangle(cornerRadius:16).stroke(.white.opacity(0.06),lineWidth:1))
   .alert(confirmCard ? (model.data?.resetCards?.pending == true ? "核对上次重置卡请求？":"确认使用 1 张重置卡？"):"重置卡操作结果",isPresented:Binding(get:{confirmCard || model.cardNotice != nil},set:{if !$0 {confirmCard=false;model.cardNotice=nil}})){
    if confirmCard {
     Button("取消",role:.cancel){}
     Button(model.data?.resetCards?.pending == true ? "确认核对重试":"确认使用 1 张"){model.consumeCard()}
    } else {Button("知道了"){model.cardNotice=nil}}
   } message:{Text(confirmCard ? "将操作当前已登录的 Codex 账号，可能消耗 1 张卡并重置符合条件的额度；成功后无法撤销，结果以官方返回为准":(model.cardNotice ?? ""))}
  }
 }
}
struct MiniWidthKey:PreferenceKey {
 static var defaultValue:CGFloat=0
 static func reduce(value:inout CGFloat,nextValue:()->CGFloat){value=max(value,nextValue())}
}
func miniRun(_ snapshot:Snapshot?)->Run? {
 let runs=snapshot?.groups ?? snapshot?.runs ?? []
 return runs.max { lhs,rhs in
  let leftRunning=lhs.status == "进行中",rightRunning=rhs.status == "进行中"
  if leftRunning != rightRunning {return !leftRunning}
  if lhs.sent != rhs.sent {return lhs.sent<rhs.sent}
  if lhs.started != rhs.started {return lhs.started<rhs.started}
  return lhs.id<rhs.id
 }
}
struct MiniCard:View {
 @ObservedObject var model:Model
 var restore:()->Void
 var resized:(CGFloat)->Void
 var body:some View {
  HStack(spacing:9){
   if let bucket=model.data?.buckets.first(where:{$0.id=="codex"}),let quota=bucket.primary {
    Text(String(format:"剩余 %.0f%%",max(0,min(100,100-quota.usedPercent))))
     .foregroundStyle(model.data?.quotaError == nil ? Color(white:0.733):Color.orange)
   } else {Text("额度读取中").foregroundStyle(.secondary)}
   Text("·").foregroundStyle(.secondary)
   if let run=miniRun(model.data) {
    let prefix=run.status == "进行中" ? "本轮 ":"最近 "
    let description=run.status == "进行中" ? "正在运行：":"最近任务："
    let partial=run.partialUsage == true ? "\n部分用量尚未返回，只汇总已知值":""
    HStack(spacing:9){
     Circle().fill(run.status == "进行中" ? Color.yellow:Color.gray).frame(width:7,height:7)
     Text(prefix+tokenText(run.usage)).foregroundStyle(runTokenColor)
      .help("\(description)\(run.title) · \(run.model) · \(run.effort)\(partial)")
    }.opacity(run.status == "进行中" ? 1:finishedRunOpacity)
   } else {Text("等待任务").foregroundStyle(.secondary)}
   Button(action:restore){Image(systemName:"arrow.up.left.and.arrow.down.right").frame(width:28,height:28).contentShape(Rectangle())}
    .buttonStyle(.plain).accessibilityLabel("恢复用量面板").help("恢复用量面板")
  }.font(.system(size:13,weight:.medium)).monospacedDigit().lineLimit(1)
   .padding(.horizontal,12).frame(height:36).fixedSize(horizontal:true,vertical:false)
   .background(GeometryReader { proxy in Color.clear.preference(key:MiniWidthKey.self,value:proxy.size.width) })
   .onPreferenceChange(MiniWidthKey.self,perform:resized)
   .foregroundStyle(Color(white:0.733)).background(Color(white:0.155))
   .clipShape(RoundedRectangle(cornerRadius:18,style:.continuous))
   .overlay(RoundedRectangle(cornerRadius:18,style:.continuous).stroke(Color.white.opacity(0.07),lineWidth:1).allowsHitTesting(false))
   .environment(\.colorScheme,.dark)
 }
}
struct WidgetRoot:View {
 @ObservedObject var model:Model
 var minimize:()->Void;var dismiss:()->Void;var resized:(CGFloat)->Void
 var toggleShape:()->Void = {}
 var resetPosition:()->Void = {}
 var body:some View {
  if model.minimized {MiniCard(model:model,restore:minimize,resized:resized)}
  else {Card(model:model,minimize:minimize,dismiss:dismiss,toggleShape:toggleShape,resetPosition:resetPosition)}
 }
}
final class ClickHostingView<Content:View>:NSHostingView<Content> {
 override func acceptsFirstMouse(for event:NSEvent?)->Bool {true}
 override var mouseDownCanMoveWindow:Bool {false}
}
// Route border/header gestures before SwiftUI scroll views can consume their events
final class WidgetPanel:NSPanel {
 weak var resizeSurface:ResizeFrameView?
 var minimized:()->Bool={false}
 var didFinishInteraction:()->Void={}
 var didBeginInteraction:()->Void={}
 var pointerPosition:()->NSPoint={NSEvent.mouseLocation}
 private var resizing=false
 private var moving:(origin:NSPoint,mouse:NSPoint)?
 var isInteracting:Bool {resizing || moving != nil}
 func canMove(at point:NSPoint)->Bool {
  if minimized() {return point.x>=8 && point.x<frame.width-44}
  let fromTop=frame.height-point.y
  let title=fromTop>=8 && fromTop<=36 && point.x>=10 && point.x<frame.width-100
  let quota=fromTop>=36 && fromTop<=82 && point.x>=10 && point.x<frame.width-10
  return title || quota
 }
 override func sendEvent(_ event:NSEvent){
  switch event.type {
  case .leftMouseDown:
   if let surface=resizeSurface,!surface.edges(at:surface.convert(event.locationInWindow,from:nil)).isEmpty {
    resizing=true;didBeginInteraction();surface.mouseDown(with:event);return
   }
   if canMove(at:event.locationInWindow) {
    moving=(frame.origin,pointerPosition());didBeginInteraction();return
   }
  case .leftMouseDragged:
   if resizing {resizeSurface?.mouseDragged(with:event);return}
   if let start=moving {
    let mouse=pointerPosition()
    setFrameOrigin(NSPoint(x:start.origin.x+mouse.x-start.mouse.x,y:start.origin.y+mouse.y-start.mouse.y));return
   }
  case .leftMouseUp:
   if resizing {resizeSurface?.mouseUp(with:event);resizing=false;didFinishInteraction();return}
   if moving != nil {moving=nil;didFinishInteraction();return}
  default:break
  }
  super.sendEvent(event)
  // SwiftUI can reset the cursor while delivering an event to its subviews
  if (event.type == .mouseMoved || event.type == .cursorUpdate),let surface=resizeSurface {
   surface.updateResizeCursor(at:surface.convert(event.locationInWindow,from:nil))
  }
 }
}
struct ResizeEdge:OptionSet {
 let rawValue:Int
 static let top=Self(rawValue:1),left=Self(rawValue:2),bottom=Self(rawValue:4),right=Self(rawValue:8)
}
func panelSizeLimits(_ screen:NSRect)->(min:NSSize,max:NSSize) {
 let maximum=NSSize(width:floor(screen.width/3),height:min(fullPanelHeight*1.5,screen.height))
 return (NSSize(width:min(260,maximum.width),height:min(minimumPanelHeight,maximum.height)),maximum)
}
func fitPanelFrame(_ frame:NSRect,to screen:NSRect)->NSRect {
 let limits=panelSizeLimits(screen)
 let w=max(limits.min.width,min(frame.width,limits.max.width)),h=max(limits.min.height,min(frame.height,limits.max.height))
 return NSRect(x:max(screen.minX,min(frame.minX,screen.maxX-w)),y:max(screen.minY,min(frame.minY,screen.maxY-h)),width:w,height:h)
}
func codexWindowFrame()->NSRect? {
 guard let app=NSRunningApplication.runningApplications(withBundleIdentifier:"com.openai.codex").first,
       let windows=CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]],
       let originTop=NSScreen.screens.first?.frame.maxY else{return nil}
 for item in windows {
  guard (item[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value==app.processIdentifier,
        (item[kCGWindowLayer as String] as? NSNumber)?.intValue==0,
        let bounds=item[kCGWindowBounds as String] as? [String:Any],
        let r=CGRect(dictionaryRepresentation:bounds as CFDictionary),r.width>=400,r.height>=300 else{continue}
  return NSRect(x:r.minX,y:originTop-r.maxY,width:r.width,height:r.height)
 }
 return nil
}
func defaultPanelFrame(size:NSSize,screen:NSRect,host:NSRect?)->NSRect {
 let anchor=host ?? screen
 // Match the reference card's horizontal alignment, but stay at the bottom
 return fitPanelFrame(NSRect(x:anchor.maxX-12-size.width,y:anchor.minY+12,width:size.width,height:size.height),to:screen)
}
func resizedPanelFrame(_ frame:NSRect,delta:NSPoint,edges:ResizeEdge,screen:NSRect)->NSRect {
 let limits=panelSizeLimits(screen)
 var r=frame
 if edges.contains(.left) {
  r.origin.x=max(max(screen.minX,frame.maxX-limits.max.width),min(frame.minX+delta.x,frame.maxX-limits.min.width))
  r.size.width=frame.maxX-r.minX
 } else if edges.contains(.right) {r.size.width=max(limits.min.width,min(frame.width+delta.x,min(limits.max.width,screen.maxX-frame.minX)))}
 if edges.contains(.bottom) {
  r.origin.y=max(max(screen.minY,frame.maxY-limits.max.height),min(frame.minY+delta.y,frame.maxY-limits.min.height))
  r.size.height=frame.maxY-r.minY
 } else if edges.contains(.top) {r.size.height=max(limits.min.height,min(frame.height+delta.y,min(limits.max.height,screen.maxY-frame.minY)))}
 return r
}
final class ResizeFrameView:NSView {
 var enabled:()->Bool={true};var finished:()->Void={}
 var pointerPosition:()->NSPoint={NSEvent.mouseLocation}
 private var drag:(frame:NSRect,mouse:NSPoint,edges:ResizeEdge,screen:NSRect)?
 private var hoverTracking:NSTrackingArea?
 private var ownsResizeCursor=false
 override var mouseDownCanMoveWindow:Bool {false}
 override func acceptsFirstMouse(for event:NSEvent?)->Bool {true}
 func edges(at p:NSPoint)->ResizeEdge {
  guard enabled(),bounds.contains(p) else{return []}
  let left=p.x<18,right=p.x>bounds.maxX-18,bottom=p.y<18,top=p.y>bounds.maxY-18
  if (left || right) && (top || bottom) {return [(left ? .left:.right),(top ? .top:.bottom)]}
  if p.x<12{return .left};if p.x>bounds.maxX-12{return .right}
  if p.y<8{return .bottom};if p.y>bounds.maxY-8{return .top}
  return []
 }
 override func hitTest(_ point:NSPoint)->NSView? {
  if !edges(at:convert(point,from:superview)).isEmpty{return self}
  return super.hitTest(point)
 }
 override func updateTrackingAreas(){
  super.updateTrackingAreas()
  if let hoverTracking=hoverTracking {removeTrackingArea(hoverTracking)}
  // Cursor rectangles alone do not cover a non-key, nonactivating panel
  // activeAlways supports mouse events, but not the cursorUpdate tracking option
  let area=NSTrackingArea(rect:.zero,options:[.mouseEnteredAndExited,.mouseMoved,.activeAlways,.inVisibleRect],owner:self,userInfo:nil)
  addTrackingArea(area);hoverTracking=area
 }
 func resizeCursor(for edge:ResizeEdge)->NSCursor {
  if #available(macOS 15.0,*),let position=NSCursor.FrameResizePosition(rawValue:UInt(edge.rawValue)) {
   return NSCursor.frameResize(position:position,directions:.all)
  }
  return edge == .left || edge == .right ? .resizeLeftRight:edge == .top || edge == .bottom ? .resizeUpDown:.crosshair
 }
 func updateResizeCursor(at point:NSPoint){
  let edge=drag?.edges ?? edges(at:point)
  if !edge.isEmpty {resizeCursor(for:edge).set();ownsResizeCursor=true}
  else if ownsResizeCursor {NSCursor.arrow.set();ownsResizeCursor=false}
 }
 override func mouseEntered(with event:NSEvent){updateResizeCursor(at:convert(event.locationInWindow,from:nil))}
 override func mouseMoved(with event:NSEvent){updateResizeCursor(at:convert(event.locationInWindow,from:nil))}
 override func cursorUpdate(with event:NSEvent){updateResizeCursor(at:convert(event.locationInWindow,from:nil))}
 override func mouseExited(with event:NSEvent){
  if drag == nil,ownsResizeCursor {NSCursor.arrow.set();ownsResizeCursor=false}
 }
 override func resetCursorRects(){
  super.resetCursorRects();guard enabled() else{return}
  let w=bounds.width,h=bounds.height
  let zones:[(NSRect,ResizeEdge)]=[
   (NSRect(x:0,y:18,width:12,height:max(0,h-36)),.left),(NSRect(x:w-12,y:18,width:12,height:max(0,h-36)),.right),
   (NSRect(x:18,y:0,width:max(0,w-36),height:8),.bottom),(NSRect(x:18,y:h-8,width:max(0,w-36),height:8),.top),
   (NSRect(x:0,y:0,width:18,height:18),[.left,.bottom]),(NSRect(x:w-18,y:0,width:18,height:18),[.right,.bottom]),
   (NSRect(x:0,y:h-18,width:18,height:18),[.left,.top]),(NSRect(x:w-18,y:h-18,width:18,height:18),[.right,.top])]
  for (rect,edge) in zones {
   addCursorRect(rect,cursor:resizeCursor(for:edge))
  }
 }
 override func mouseDown(with event:NSEvent){
  guard let window=window,let screen=window.screen ?? NSScreen.main else{return}
  let edge=edges(at:convert(event.locationInWindow,from:nil));guard !edge.isEmpty else{return}
  let frame=fitPanelFrame(window.frame,to:screen.visibleFrame)
  window.setFrame(frame,display:true)
  drag=(frame,pointerPosition(),edge,screen.visibleFrame)
  updateResizeCursor(at:convert(event.locationInWindow,from:nil))
 }
 override func mouseDragged(with event:NSEvent){
  guard let drag=drag,let window=window else{return};let mouse=pointerPosition()
  let next=resizedPanelFrame(drag.frame,delta:NSPoint(x:mouse.x-drag.mouse.x,y:mouse.y-drag.mouse.y),edges:drag.edges,screen:drag.screen)
  if next != window.frame {window.setFrame(next,display:true,animate:false)}
  resizeCursor(for:drag.edges).set()
 }
 override func mouseUp(with event:NSEvent){
  guard drag != nil else{return};drag=nil;window?.invalidateCursorRects(for:self);finished()
  if let window=window {updateResizeCursor(at:convert(window.convertPoint(fromScreen:pointerPosition()),from:nil))}
 }
}
final class Delegate:NSObject,NSApplicationDelegate,NSWindowDelegate {
 let model=Model();var panel:NSPanel!;var border:ResizeFrameView!;var statusItem:NSStatusItem!;var hiddenByUser=false;var observer:NSObjectProtocol?;var fullFrame:NSRect?;var miniOrigin:NSPoint?
 var expandedSize=NSSize(width:defaultPanelWidth,height:fullPanelHeight)
 var compactSize=NSSize(width:defaultPanelWidth,height:minimumPanelHeight)
 var defaultAnchored=true
 private var anchorTimer:Timer?
 private let pageMonitor=ChatPageMonitor()
 func applicationDidFinishLaunching(_ notification:Notification){
  NSApp.setActivationPolicy(.accessory)
  panel=WidgetPanel(contentRect:NSRect(x:0,y:0,width:defaultPanelWidth,height:fullPanelHeight),styleMask:[.borderless,.nonactivatingPanel],backing:.buffered,defer:false)
  panel.level = .floating;panel.isFloatingPanel=true;panel.hidesOnDeactivate=false;panel.isMovableByWindowBackground=false;panel.backgroundColor = .clear;panel.isOpaque=false;panel.hasShadow=true
  panel.acceptsMouseMovedEvents=true
  panel.collectionBehavior=[.canJoinAllSpaces,.fullScreenAuxiliary];panel.title="Codex用量浮窗"
  let host=ClickHostingView(rootView:WidgetRoot(model:model,minimize:{[weak self] in self?.minimize()},dismiss:{[weak self] in self?.hideWidget()},resized:{[weak self] width in self?.resizeMini(width)},toggleShape:{[weak self] in self?.toggleShape()},resetPosition:{[weak self] in self?.resetPosition()}))
  if #available(macOS 13.0,*) {host.sizingOptions=[]}
  border=ResizeFrameView(frame:panel.contentRect(forFrameRect:panel.frame));host.frame=border.bounds;host.autoresizingMask=[.width,.height];border.addSubview(host)
  border.enabled={[weak self] in self?.model.minimized == false}
  border.finished={}
  panel.contentView=border;panel.delegate=self
  if let widget=panel as? WidgetPanel {
   widget.resizeSurface=border;widget.minimized={[weak self] in self?.model.minimized == true}
   widget.didBeginInteraction={[weak self] in self?.model.interactionActive=true;self?.defaultAnchored=false}
   widget.didFinishInteraction={[weak self] in
    guard let self=self else{return}
    self.model.finishInteraction()
    if !self.model.minimized,let screen=self.panel.screen {
     let next=fitPanelFrame(self.panel.frame,to:screen.visibleFrame)
     if next != self.panel.frame {self.panel.setFrame(next,display:true,animate:false)}
     self.savePanelSize()
    }
    self.updateVisibility()
   }
  }
  if let screen=NSScreen.main {
   let r=screen.visibleFrame,defaults=UserDefaults.standard
   let w=defaults.double(forKey:"fullPanelWidth"),h=defaults.double(forKey:"fullPanelHeight")
   let migrateWidth = !defaults.bool(forKey:"resizeLayoutV3")
   let migrateCompact = !defaults.bool(forKey:"dualTaskLayoutV2")
   let savedExpandedHeight=defaults.double(forKey:"expandedPanelHeight")
   let restoredHeight = migrateCompact ? (savedExpandedHeight>=360 ? savedExpandedHeight:fullPanelHeight):h
   let size=NSSize(width:migrateWidth ? defaultPanelWidth:(w>0 ? w:defaultPanelWidth),height:restoredHeight>0 ? restoredHeight:fullPanelHeight)
   panel.setFrame(defaultPanelFrame(size:size,screen:r,host:codexWindowFrame()),display:true)
   let ew=defaults.double(forKey:"expandedPanelWidth"),eh=defaults.double(forKey:"expandedPanelHeight")
   let cw=defaults.double(forKey:"compactPanelWidth"),ch=defaults.double(forKey:"compactPanelHeight")
   if ew>0 && eh>=360 {expandedSize=NSSize(width:migrateWidth ? defaultPanelWidth:ew,height:eh)}
   if cw>0 && ch>0 && ch<360 {compactSize=NSSize(width:migrateWidth ? defaultPanelWidth:cw,height:migrateCompact ? minimumPanelHeight:max(minimumPanelHeight,ch))}
   syncPanelMode()
   if migrateCompact {savePanelSize();defaults.set(true,forKey:"dualTaskLayoutV2")}
   if migrateWidth {
    defaults.set(defaultPanelWidth,forKey:"expandedPanelWidth");defaults.set(defaultPanelWidth,forKey:"compactPanelWidth")
    savePanelSize();defaults.set(true,forKey:"resizeLayoutV3")
   }
  }
  statusItem=NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength)
  statusItem.button?.title="◉ 用量"
  let menu=NSMenu()
  let show=NSMenuItem(title:"显示用量浮窗",action:#selector(showWidget),keyEquivalent:"");show.target=self;menu.addItem(show)
  let access=NSMenuItem(title:"授权聊天页识别…",action:#selector(requestPageAccess),keyEquivalent:"");access.target=self;menu.addItem(access)
  let quit=NSMenuItem(title:"退出用量统计",action:#selector(quitWidget),keyEquivalent:"");quit.target=self;menu.addItem(quit)
  statusItem.menu=menu
  observer=NSWorkspace.shared.notificationCenter.addObserver(forName:NSWorkspace.didActivateApplicationNotification,object:nil,queue:.main){[weak self] _ in self?.updateVisibility()}
  anchorTimer=Timer.scheduledTimer(withTimeInterval:0.5,repeats:true){[weak self] _ in self?.updateVisibility()}
  updateVisibility();model.start()
  if CommandLine.arguments.contains("--request-accessibility") {requestPageAccess()}
 }
 func alignDefaultPosition(){
  guard defaultAnchored,!hiddenByUser,!model.minimized,(panel as? WidgetPanel)?.isInteracting != true,
        NSWorkspace.shared.frontmostApplication?.bundleIdentifier=="com.openai.codex",
        let host=codexWindowFrame(),let screen=NSScreen.screens.max(by:{
         $0.frame.intersection(host).width*$0.frame.intersection(host).height < $1.frame.intersection(host).width*$1.frame.intersection(host).height
        }) else{return}
  let next=defaultPanelFrame(size:panel.frame.size,screen:screen.visibleFrame,host:host)
  if next != panel.frame {panel.setFrame(next,display:true,animate:false)}
 }
 func resetPosition(){
  let diagnostics=UserDefaults.standard.bool(forKey:"positionDiagnostics")
  if diagnostics {UserDefaults.standard.set(["requested":Date().timeIntervalSince1970,"interacting":(panel as? WidgetPanel)?.isInteracting == true,"minimized":model.minimized],forKey:"lastPositionReset")}
  guard !model.minimized,(panel as? WidgetPanel)?.isInteracting != true else{return}
  let host=codexWindowFrame()
  let hostScreen=host.flatMap { frame in
   NSScreen.screens.filter{$0.frame.intersects(frame)}.max(by:{
    $0.frame.intersection(frame).width*$0.frame.intersection(frame).height < $1.frame.intersection(frame).width*$1.frame.intersection(frame).height
   })
  }
  guard let screen=hostScreen ?? panel.screen ?? NSScreen.main else{return}
  let previous=panel.frame
  defaultAnchored=true
  panel.setFrame(defaultPanelFrame(size:panel.frame.size,screen:screen.visibleFrame,host:host),display:true,animate:false)
  if diagnostics {UserDefaults.standard.set(["before":NSStringFromRect(previous),"after":NSStringFromRect(panel.frame),"anchored":defaultAnchored],forKey:"lastPositionReset")}
  savePanelSize();panel.invalidateCursorRects(for:border)
 }
 func updateVisibility(){
  if (panel as? WidgetPanel)?.isInteracting == true {return}
  let frontmost=NSWorkspace.shared.frontmostApplication
  let id=frontmost?.bundleIdentifier
  if id==Bundle.main.bundleIdentifier {return}
  var page:ChatPageState = .other
  if id=="com.openai.codex",let pid=frontmost?.processIdentifier,!hiddenByUser {
   page=pageMonitor.current(for:pid)
   pageMonitor.refresh(pid:pid){[weak self] in self?.updateVisibility()}
  }
  statusItem.button?.toolTip=AXIsProcessTrusted() ? "仅在聊天界面显示；设置和其他页面自动隐藏":"需要辅助功能权限识别聊天页，请从菜单选择“授权聊天页识别”"
  if shouldShowUsagePanel(frontmost:id,hiddenByUser:hiddenByUser,page:page) {
   alignDefaultPosition();if !panel.isVisible {panel.orderFrontRegardless()}
  } else if panel.isVisible {panel.orderOut(nil)}
 }
 @objc func requestPageAccess(){
  // System permission is requested only through the user's explicit action
  let options=[kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String:true] as CFDictionary
  _=AXIsProcessTrustedWithOptions(options)
  updateVisibility()
 }
 func resizeMini(_ width:CGFloat){
  if (panel as? WidgetPanel)?.isInteracting == true {return}
  guard model.minimized,width>0,abs(panel.frame.width-width)>0.5 else{return}
  let frame=panel.frame
  let visible=(panel.screen ?? NSScreen.main)?.visibleFrame ?? frame
  let x=max(visible.minX,min(frame.midX-width/2,visible.maxX-width))
  panel.setFrame(NSRect(x:x,y:frame.minY,width:width,height:36),display:true)
 }
 func minimize(){
  if model.minimized {
   miniOrigin=panel.frame.origin;model.minimized=false
   if let frame=fullFrame,let screen=panel.screen ?? NSScreen.main {panel.setFrame(fitPanelFrame(frame,to:screen.visibleFrame),display:true)}
  } else {
   fullFrame=panel.frame
   let screen=panel.screen ?? NSScreen.main
   let visible=screen?.visibleFrame ?? NSRect(x:0,y:0,width:1200,height:800)
   // Initial position only: no claim of tracking Codex's composer bounds
   let desired=miniOrigin ?? NSPoint(x:visible.minX+visible.width*0.48-185,y:visible.minY+visible.height*0.31)
   let origin=NSPoint(x:max(visible.minX,min(desired.x,visible.maxX-370)),y:max(visible.minY,min(desired.y,visible.maxY-36)))
   model.minimized=true
   panel.setFrame(NSRect(origin:origin,size:NSSize(width:370,height:36)),display:true)
  }
  panel.invalidateCursorRects(for:border)
 }
 func savePanelSize(){
  guard !model.minimized else{return};fullFrame=panel.frame
  syncPanelMode()
  UserDefaults.standard.set(panel.frame.width,forKey:"fullPanelWidth")
  UserDefaults.standard.set(panel.frame.height,forKey:"fullPanelHeight")
  let prefix=model.compact ? "compactPanel":"expandedPanel"
  if model.compact {compactSize=panel.frame.size}else{expandedSize=panel.frame.size}
  UserDefaults.standard.set(panel.frame.width,forKey:prefix+"Width")
  UserDefaults.standard.set(panel.frame.height,forKey:prefix+"Height")
 }
 func syncPanelMode(){
  guard !model.minimized,(panel as? WidgetPanel)?.isInteracting != true else{return}
  let compact=panel.frame.height<360
  if model.compact != compact {model.compact=compact}
 }
 func windowDidResize(_ notification:Notification){syncPanelMode()}
 func toggleShape(){
  guard !model.minimized,let screen=panel.screen ?? NSScreen.main else{return}
  savePanelSize()
  let size=model.compact ? expandedSize:compactSize
  let old=panel.frame
  panel.setFrame(fitPanelFrame(NSRect(x:old.maxX-size.width,y:old.minY,width:size.width,height:size.height),to:screen.visibleFrame),display:true)
  alignDefaultPosition()
  savePanelSize();panel.invalidateCursorRects(for:border)
 }
 func windowDidChangeScreen(_ notification:Notification){
  if (panel as? WidgetPanel)?.isInteracting == true {return}
  guard !model.minimized,let screen=panel.screen else{return}
  let fitted=fitPanelFrame(panel.frame,to:screen.visibleFrame)
  if fitted != panel.frame {panel.setFrame(fitted,display:true)}
 }
 func hideWidget(){hiddenByUser=true;panel.orderOut(nil)}
 func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool {
  showWidget();return false
 }
 @objc func showWidget(){
  hiddenByUser=false;defaultAnchored=true
  if model.minimized {minimize()}
  if let host=NSRunningApplication.runningApplications(withBundleIdentifier:"com.openai.codex").first {host.activate(options:[.activateIgnoringOtherApps])}
  updateVisibility()
 }
 @objc func quitWidget(){NSApp.terminate(nil)}
 func applicationWillTerminate(_ notification:Notification){anchorTimer?.invalidate();if let observer=observer{NSWorkspace.shared.notificationCenter.removeObserver(observer)};model.stop()}
}
#if !WIDGET_TESTS
@main struct UsageWidgetApp {
 static func main(){
  let app=NSApplication.shared
  let delegate=Delegate()
  app.delegate=delegate
  withExtendedLifetime(delegate){app.run()}
 }
}
#endif
