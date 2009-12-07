from datetime import datetime
from os.path import exists
import httplib, time, xml.dom
from urllib import splithost, splittype, urlopen
import xml.dom.minidom as minidom

NS = {
    "wsdl":         "http://schemas.xmlsoap.org/wsdl/",
    "wsdlsoap":     "http://schemas.xmlsoap.org/wsdl/soap/",
    "wsdlsoap12":   "http://schemas.xmlsoap.org/wsdl/soap12/",
    "soap":         "http://schemas.xmlsoap.org/soap/envelope/"
}

USERAGENT = "minisoap/0.1"
DEBUG = False
TIMEOUT = 60

class SoapException(Exception):
    pass

class WsdlException(Exception):
    pass

class ServiceException(Exception):
    pass

class Service:
    """Parse a WSDL file for a service and answer requests to the defined operations. Only works with document/literal-style services (e.g. Google) because types are almost completely ignored."""
    def __init__(self, wsdlfile, headers=None):
        self.operations = {}
        if not exists(wsdlfile):
            raise IOError(u"WSDL file %s does not exist" % wsdlfile)
        self.wsdlfile = wsdlfile
        self._loadWSDL(wsdlfile)
        if headers:
            self.addHeadersToAll(headers)
        
    def __repr__(self):
        return "Proxy for web service %s" % self.wsdlfile

    def _loadWSDL(self, wsdlfile):
        """Read and parse the WSDL file"""
        self.wsdl = minidom.parse(open(self.wsdlfile))

        targetNamespace = self.wsdl.documentElement.getAttribute("targetNamespace")
        
        self._getServices(targetNamespace)

    def _getServices(self, targetNamespace):
        """Parse information about the WSDL service"""
        global NS
        services = self.wsdl.getElementsByTagNameNS(NS["wsdl"], "service")

        for svc in services:
            self._getPorts(svc, targetNamespace)
                
    def _getPorts(self, service, targetNamespace):
        global NS
        ports = service.getElementsByTagNameNS(NS["wsdl"], "port")
        for p in ports:
            name = p.getAttribute("name")
            binding = stripNs(p.getAttribute("binding"))
    
            addrs10 = p.getElementsByTagNameNS(NS["wsdlsoap"], "address")
            addrs12 = p.getElementsByTagNameNS(NS["wsdlsoap12"], "address")
            addrs = addrs10 + addrs12
            if not len(addrs):
                raise WsdlException("No service addresses found")
            else:
                uri = addrs[0].getAttribute("location")

            # get bindings for each port
            self._getBindings(targetNamespace, name, binding, uri)
            
    def _getBindings(self, targetNamespace, port, binding, uri):
        global NS
        bindings = self.wsdl.getElementsByTagNameNS(NS["wsdl"], "binding")

        for bing in bindings:
            if bing.getAttribute("name") == binding:
                self._getOperations(targetNamespace, port, binding, uri, bing)
        
    def _getOperations(self, targetNamespace, port, binding, uri, bindingNode):
        """Parse the WSDL bindings section"""
        global NS
        operations = bindingNode.getElementsByTagNameNS(NS["wsdl"], "operation")

        for op in operations:
            opname = op.getAttribute("name")
            self.operations[opname] = Operation(op, targetNamespace, port,
                    binding, uri)

    def addHeadersToAll(self, headers):
        for op in self.operations.values():
            op.addHeaders(headers)

    def addNamespaceToAll(self, name, value):
        for op in self.operations.values():
            op.addNamespaceToAll(name, value)

    def __getattr__(self, name):
        if name in self.operations:
            return self.operations[name]
        else:
            raise KeyError(name)

class Operation:
    def __init__(self, opnode, targetNs, port, binding, uri):
        global NS
    
        self._headers = {}
        self.requestNamespaces = {}

        self.requestDoc = None
        self.responseDoc = None

        self.name = opnode.getAttribute("name")

        self.targetNs = targetNs
        self.port = port
        self.binding = binding
        self.uri = uri

        # get soapAction
        ops10 = opnode.getElementsByTagNameNS(NS["wsdlsoap"], "operation")
        ops12 = opnode.getElementsByTagNameNS(NS["wsdlsoap12"], "operation")
        ops = ops10 + ops12
        if len(ops):
            self.soapAction = ops[0].getAttribute("soapAction")
        else:
            self.soapAction = ""

        # get output node name, for finessing output a bit
        self.responseNodeName = ""
        output = opnode.getElementsByTagNameNS(NS["wsdl"], "output")
        if len(output) and output[0].hasAttribute("name"):
            self.responseNodeName = output[0].getAttribute("name")

    def __repr__(self):
        return "Proxy for web service operation %s" % self.name

    def addHeader(self, name, value):
        self._headers[name] = value

    def addHeaders(self, headers):
        self._headers.update(headers)
        
    def addNamespace(self, name, value):
        self.requestNamespaces[name] = value
        
    def __call__(self, **kwargs):
        request = self.makeRequest(kwargs)
        response = self.sendRequest(request)
        return self.parseResponse(response)
        
    def makeRequest(self, params):
        """Create and serialise SOAP envelope from a DOM document"""
        global NS
        impl = minidom.getDOMImplementation()
        self.requestDoc = impl.createDocument(self.targetNs, "soap:Envelope", None)
        setDocAttr = self.requestDoc.documentElement.setAttribute
        
        setDocAttr("xmlns:soap", NS["soap"])

        for name, value in self.requestNamespaces.iteritems():
            setDocAttr("xmlns:" + name, value)

        setDocAttr("xmlns", self.targetNs)

        # create header
        if len(self._headers):
            header = self.requestDoc.createElementNS(NS["soap"], "soap:Header")
            self.addLiteral(self.requestDoc, header, self._headers)
            self.requestDoc.documentElement.appendChild(header)

        # and body
        body = self.requestDoc.createElementNS(NS["soap"], "soap:Body")
        self.requestDoc.documentElement.appendChild(body)
        
        operation = self.requestDoc.createElement(self.name)
        if type(params) == dict:
            self.addLiteral(self.requestDoc, operation, params)
        body.appendChild(operation)
    
        return self.requestDoc.toxml()
    
    def addLiteral(self, doc, node, data):
        """Recursively translate dictionaries into XML nodes"""
        for name, value in data.iteritems():
            if name[0:1] == "@":
                node.setAttribute(name[1:], str(value))
            else:
                el = doc.createElement(name)
                
                if type(value) in (list, tuple):
                    for v in value:
                        self.addLiteral(doc, node, { name: v })
                else:
                    self.addValue(doc, el, value)
                    node.appendChild(el)
                
    def addValue(self, doc, el, value):
        if type(value) == dict: self.addLiteral(doc, el, value)
        else: el.appendChild(doc.createTextNode(str(value)))
    
    def sendRequest(self, request):
        """Push a request down a pipe to a HTTP/HTTPS server"""
        global USERAGENT, DEBUG

        protocol, uri = splittype(self.uri)
        host, path = splithost(uri)
        h = None
        response = None

        if (protocol == "https"):
            h = httplib.HTTPSConnection(host)
        else:
            h = httplib.HTTPConnection(host)
        
        if DEBUG:
            h.set_debuglevel(1)
        h.connect()
        h.sock.settimeout(TIMEOUT)

        headers ={"User-Agent": USERAGENT, "Content-Type": "text/xml"}
        if self.soapAction:
            headers["SOAPAction"] = self.soapAction

        h.request("POST", path, request, headers)

        # stolen from soapy - thanks!
        while True:
            response = h.getresponse()
            if response.status != 100: break
            h._HTTPConnection__state = httplib._CS_REQ_SENT
            h._HTTPConnection__response = None
            
        output = response.read()

        if DEBUG:
            print "reply:", `output`
        # return response text
        return output

    def parseResponse(self, response):
        """Take a response XML document and turn it into a dictionary"""
        global NS
        
        self.responseDoc = minidom.parseString(response)
        
        bodies = self.responseDoc.getElementsByTagNameNS(NS["soap"], "Body")
        if not len(bodies):
            raise SoapException("No body in response")
        
        body = bodies[0]
        
        # catch a fault
        faultstrings = body.getElementsByTagName("faultstring")
        if len(faultstrings):
            raise ServiceException(faultstrings[0].firstChild.nodeValue)
        
        responseData = self.parseLiteral(body.childNodes)

        if self.responseNodeName and self.responseNodeName in responseData.keys():
            innerResp = responseData[self.responseNodeName]
            if len(innerResp) == 1:
                return innerResp.values()[0]
            else:
                return innerResp
        else:
            return responseData

    def parseLiteral(self, nodes):
        """Process document/literal XML into a data structure composed from dictionaries, lists and values"""
        d = {}

        for n in nodes:
            if n.nodeType == xml.dom.Node.ELEMENT_NODE:
                if len(n.childNodes) == 1 and n.firstChild.nodeType == xml.dom.Node.TEXT_NODE:
                    value = n.firstChild.nodeValue
                else:
                    value = self.parseLiteral(n.childNodes)
                
                # add a value to a dictionary, creating lists if multiple values
                # have the same key
                if n.nodeName in d:
                    if type(d[n.nodeName]) == list:
                        d[n.nodeName].append(value)
                    else:
                        d[n.nodeName] = [d[n.nodeName], value]
                else:
                    d[n.nodeName] = value

        return d
    
def stripNs(str):
    """Strip a namespace prefix from a string"""
    return str.split(":")[-1]

def iso2datetime(iso):
    t = time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    return datetime(t[0], t[1], t[2], t[3], t[4], t[5], 0, None)
