import java.io.FileOutputStream;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;
import unluac.Configuration;
import unluac.Main;
import unluac.decompile.Decompiler;
import unluac.decompile.FileOutputProvider;
import unluac.decompile.Output;
import unluac.parse.LFunction;

/**
 * Decompile every function on its own (children stubbed) and write it to <outdir>/<index>.lua.
 * Usage: java FnPrint <in.luac> <outdir> [nogoto|patched]
 */
public class FnPrint {
  public static void main(String[] args) throws Exception {
    Configuration config = new Configuration();
    Method m = Main.class.getDeclaredMethod("file_to_function", String.class, Configuration.class);
    m.setAccessible(true);
    LFunction main = (LFunction) m.invoke(null, args[0], config);
    String mode = args.length > 2 ? args[2] : "nogoto";
    if (mode.equals("patched")) Patch.allowGoto(main.header.version);
    else Patch.allowBreakRewrite(main.header.version);
    List<LFunction> all = new ArrayList<>();
    FnCheck.preorder(main, all);
    for (int i = 0; i < all.size(); i++) {
      LFunction f = all.get(i);
      LFunction[] kids = f.functions;
      LFunction[] stubs = new LFunction[kids.length];
      for (int k = 0; k < kids.length; k++) stubs[k] = FnCheck.stub(f, kids[k]);
      f.functions = stubs;
      try {
        Decompiler d = new Decompiler(f);
        Decompiler.State st = d.decompile();
        Output out = new Output(new FileOutputProvider(new FileOutputStream(args[1] + "/" + i + ".lua")));
        d.print(st, out);
        out.finish();
      } catch (Throwable t) {
        System.out.println("FAIL " + i + " " + t);
      } finally {
        f.functions = kids;
      }
    }
  }
}
