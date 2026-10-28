module counter_sva(input logic clk, input logic rst, input logic en, input logic [3:0] count);
  logic seen = 1'b0;
  always @(posedge clk) if (rst) seen <= 1'b1;

  // reset forces 0 (priority over en)
  assert property (@(posedge clk) rst |=> count == 4'd0);
  // increment with wrap modulo 16
  assert property (@(posedge clk) seen && !rst && en |=> count == $past(count) + 4'd1);
  // hold when disabled
  assert property (@(posedge clk) seen && !rst && !en |=> count == $past(count));
endmodule
