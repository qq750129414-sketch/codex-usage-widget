import AppKit
import ApplicationServices

enum ChatPageState { case chat, other, unavailable }

func isChatComposer(role:String, label:String)->Bool {
 let labels:Set<String>=["随心输入","隨心輸入","Ask anything","Message Codex","Message ChatGPT"]
 return role == kAXTextAreaRole && labels.contains(label.trimmingCharacters(in:.whitespacesAndNewlines))
}

func shouldShowUsagePanel(frontmost:String?,hiddenByUser:Bool,page:ChatPageState)->Bool {
 !hiddenByUser && frontmost == "com.openai.codex" && page == .chat
}

// Reads only UI roles, composer labels and geometry, never AXValue/chat text
final class ChatPageMonitor {
 private let queue=DispatchQueue(label:"usage-widget.page-visibility",qos:.utility)
 private var busy=false
 private var requestedAt:TimeInterval=0
 private var sampledAt:TimeInterval=0
 private var sampledPID:pid_t?
 private var state:ChatPageState = .unavailable

 func current(for pid:pid_t)->ChatPageState {
  guard sampledPID == pid,ProcessInfo.processInfo.systemUptime-sampledAt<2 else{return .unavailable}
  return state
 }

 func refresh(pid:pid_t,completed:@escaping()->Void){
  let now=ProcessInfo.processInfo.systemUptime
  guard !busy,now-requestedAt>=0.75 else{return}
  busy=true;requestedAt=now
  queue.async { [weak self] in
   let result=Self.inspect(pid:pid)
   DispatchQueue.main.async {
    guard let self=self else{return}
    self.busy=false;self.sampledPID=pid;self.sampledAt=ProcessInfo.processInfo.systemUptime;self.state=result
    completed()
   }
  }
 }

 private static func attribute(_ element:AXUIElement,_ name:String)->CFTypeRef? {
  var value:CFTypeRef?
  return AXUIElementCopyAttributeValue(element,name as CFString,&value) == .success ? value:nil
 }

 static func inspect(pid:pid_t)->ChatPageState {
  guard AXIsProcessTrusted() else{return .unavailable}
  let app=AXUIElementCreateApplication(pid)
  AXUIElementSetMessagingTimeout(app,0.08)
  guard let value=attribute(app,kAXFocusedWindowAttribute),CFGetTypeID(value)==AXUIElementGetTypeID() else{return .other}
  let window=value as! AXUIElement
  if let sheets=attribute(window,"AXSheets") as? [AXUIElement],!sheets.isEmpty {return .other}
  var nodes:[(AXUIElement,Int,Bool)]=[(window,0,false)]
  var index=0
  let began=ProcessInfo.processInfo.systemUptime
  let leaves:Set<String>=[kAXStaticTextRole,kAXButtonRole,kAXCheckBoxRole,kAXRadioButtonRole,kAXImageRole,kAXMenuBarRole,kAXMenuRole,kAXToolbarRole,"AXHeading","AXLink","AXPopUpButton"]
  while index<nodes.count,index<2400 {
   guard ProcessInfo.processInfo.systemUptime-began<0.30 else{return .unavailable}
   let (element,depth,insideMainPage)=nodes[index];index+=1
   guard let role=attribute(element,kAXRoleAttribute) as? String else{continue}
   var mainPage=insideMainPage
   if role == "AXWebArea" {
    let url=attribute(element,"AXURL").map{String(describing:$0)} ?? ""
    // Exclude browser tabs, previews and embedded third-party applications
    guard url == "app://-/index.html" else{continue}
    mainPage=true
   }
   if mainPage && role == kAXTextAreaRole {
    let label=attribute(element,kAXDescriptionAttribute) as? String ?? ""
    if isChatComposer(role:role,label:label),attribute(element,"AXHidden") as? Bool != true {return .chat}
   }
   if depth<45,!leaves.contains(role),let children=attribute(element,kAXChildrenAttribute) as? [AXUIElement] {
    nodes.append(contentsOf:children.map{($0,depth+1,mainPage)})
   }
  }
  return index<nodes.count ? .unavailable:.other
 }
}
